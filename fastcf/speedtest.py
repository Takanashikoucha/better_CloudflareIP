# -*- coding: utf-8 -*-
"""443/TLS 下载测速 + 实际服务节点探测（speed.cloudflare.com/__down）。

- speed_test(ip, bytes, secs)：单次连接完成下载测速，速度取时长内峰值
- probe_location(ip)：极小流量请求读 cf-meta-* 头，得到实际服务地
所有流量直连（模块导入时已清除代理环境变量）。
"""
import socket
import ssl
import time

from . import config

_SSL_CTX = None


def _ssl_ctx() -> ssl.SSLContext:
    """进程级单例 SSL 上下文（跳过主机名/证书校验，仅用于读响应头）。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX = ctx
    return _SSL_CTX


def parse_cf_headers(head_bytes: bytes) -> dict:
    """解析 CF 响应头，返回 {lowercase_key: value}。"""
    meta = {}
    head_str = head_bytes.split(b"\r\n\r\n", 1)[0].decode("latin-1", "ignore")
    for line in head_str.split("\r\n"):
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta


def _read_head(conn, timeout: float) -> bytes:
    """读响应头（到 \r\n\r\n 为止）。"""
    conn.settimeout(timeout)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            break
        head += chunk
    return head if b"\r\n\r\n" in head else b""


def probe_location(ip: str, timeout: float = 4) -> tuple:
    """探测单个 IP 的**实际服务地**（CF IP 全球共享池，注册归属 ≠ 实际服务地）。

    返回 (country_code, colo, city)；失败返回 (None, None, None)。
    """
    conn = None
    try:
        sock = socket.create_connection((ip, config.SPEED_PORT), timeout=timeout)
        conn = _ssl_ctx().wrap_socket(sock, server_hostname=config.SPEED_HOST)
        conn.settimeout(timeout + 4)
        req = (f"GET /__down?bytes=65536 HTTP/1.1\r\n"
               f"Host: {config.SPEED_HOST}\r\nUser-Agent: Mozilla/5.0 (FastCF)\r\n"
               f"Connection: close\r\n\r\n").encode()
        conn.sendall(req)
        head = _read_head(conn, timeout + 4)
        if not head:
            return (None, None, None)
        meta = parse_cf_headers(head)
        cc = (meta.get("cf-meta-country") or meta.get("country") or "").upper()
        colo = meta.get("cf-meta-colo") or meta.get("colo") or ""
        city = meta.get("cf-meta-city") or meta.get("city") or ""
        if not cc:
            return (None, None, None)
        return (cc, colo, city)
    except Exception:
        return (None, None, None)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _download_worker(ip: str, speed_bytes: int, speed_secs: float,
                     is_cancelled, result: dict, conn_idx: int,
                     total_conns: int, peak_bps: list, win_bytes: list,
                     win_start: list, global_start: list, slow_bytes: list,
                     slow_chunks: list):
    """单个连接的下载 worker（多线程并发）。"""
    conn = None
    try:
        t0 = time.perf_counter()
        sock = socket.create_connection((ip, config.SPEED_PORT), timeout=3)
        conn = _ssl_ctx().wrap_socket(sock, server_hostname=config.SPEED_HOST)
        if conn_idx == 0:
            result["ping"] = max(1, int((time.perf_counter() - t0) * 1000))
        conn.settimeout(5)

        req = (f"GET /__down?bytes={speed_bytes} HTTP/1.1\r\n"
               f"Host: {config.SPEED_HOST}\r\nUser-Agent: Mozilla/5.0 (FastCF)\r\n"
               f"Connection: close\r\n\r\n").encode()
        conn.sendall(req)
        head = _read_head(conn, 5)
        if conn_idx == 0 and head:
            meta = parse_cf_headers(head)
            cf_ray = meta.get("cf-ray", "")
            if cf_ray:
                result["cfRay"] = cf_ray
                parts = cf_ray.split("-")
                if len(parts) >= 2:
                    result["dc"] = parts[-1].strip()
            for k in ("country", "city", "colo"):
                if k in meta and f"cf-meta-{k}" not in meta:
                    meta[f"cf-meta-{k}"] = meta[k]
            loc_parts = []
            country_code = meta.get("cf-meta-country", "")
            city = meta.get("cf-meta-city", "")
            if country_code:
                loc_parts.append(country_code)
            if city and city not in ("0", "N/A"):
                loc_parts.append(city)
            if loc_parts:
                result["location"] = "·".join(loc_parts)

        # 下载循环（共享滑动窗口）
        buf = bytearray(65536)
        while time.time() - global_start[0] < speed_secs:
            if is_cancelled and is_cancelled():
                break
            try:
                n = conn.recv_into(buf)
            except socket.timeout:
                if is_cancelled and is_cancelled():
                    break
                continue
            except Exception:
                break
            if not n:
                break
            now = time.time()
            if now - global_start[0] < config.SPEED_SLOW_START_SECS:
                slow_bytes[0] += n
                slow_chunks[0] += 1
            win_bytes[0] += n
            if now - win_start[0] >= 1.0:
                bps = win_bytes[0] * 8 / (now - win_start[0])
                if bps > peak_bps[0]:
                    peak_bps[0] = bps
                win_bytes[0], win_start[0] = 0, now
            if now - global_start[0] >= config.SPEED_SLOW_START_SECS:
                if slow_chunks[0] >= 1 and slow_bytes[0] < config.SPEED_SLOW_START_BYTES:
                    break
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def speed_test(ip: str, speed_bytes: int, speed_secs: float,
               is_cancelled=None) -> dict:
    """多连接并发 443/TLS 下载测速（绕过 GFW 单连接限速）。

    返回 {ip, port, ping, mbps, dc, cfRay, location}。
    mbps 为时长内峰值（所有连接合计）；ping 为第一个连接的 TCP+TLS 时延。
    is_cancelled 为可调用对象，返回 True 时提前结束。
    """
    result = {"ip": ip, "port": config.SPEED_PORT, "ping": 0, "mbps": 0,
              "dc": "", "cfRay": "", "location": ""}

    # 共享状态（列表包装，线程间共享）
    peak_bps = [0.0]
    win_bytes = [0]
    win_start = [time.time()]
    global_start = [time.time()]
    slow_bytes = [0]
    slow_chunks = [0]

    import threading
    threads = []
    for i in range(config.SPEED_CONNS):
        t = threading.Thread(
            target=_download_worker,
            args=(ip, speed_bytes, speed_secs, is_cancelled, result, i,
                  config.SPEED_CONNS, peak_bps, win_bytes, win_start,
                  global_start, slow_bytes, slow_chunks),
            daemon=True
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # 观察窗口内零字节 → 直接 0
    if slow_chunks[0] == 0 and peak_bps[0] == 0 and win_bytes[0] == 0:
        return result
    result["mbps"] = int(peak_bps[0] / 1_000_000)
    return result
