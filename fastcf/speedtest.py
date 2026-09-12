# -*- coding: utf-8 -*-
"""443/TLS 下载测速 + 实际服务节点探测（speed.cloudflare.com/__down）。

- speed_test(ip, bytes, secs)：多连接并发下载测速，速度 = 实际下载大小 / min(实际下载时间, 设定时间)
- probe_location(ip)：极小流量请求读 cf-meta-* 头，得到实际服务地
所有流量直连（模块导入时已清除代理环境变量）。

关键设计：
- /__down?bytes=N 只服务 N 字节就 EOF；EOF 后**不重开连接**，而是用实际下载时间计算速度
- 速度 = 实际下载大小 / min(实际下载时间, 设定时间)
  - 如果下载时间 < 设定时间（提前 EOF）：速度 = 实际下载大小 / 实际下载时间
  - 如果下载时间 > 设定时间（超时）：速度 = 实际下载大小 / 设定时间
- 4 连接并发，总速度 = 各连接速度之和（或总下载量/总时间）
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


def _open_conn(ip: str, speed_bytes: int, result: dict, conn_idx: int):
    """建立 TCP+TLS 连接并发送 GET 请求，返回 (conn, head)。失败抛异常。

    如果收到 429（限速），自动降级到 5MB（限速阈值）重试。
    """
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
    # 检查是否被限速（429）
    if head and b"429" in head.split(b"\r\n", 1)[0]:
        # 降级到 5MB（限速阈值）重试
        try:
            conn.close()
        except Exception:
            pass
        sock = socket.create_connection((ip, config.SPEED_PORT), timeout=3)
        conn = _ssl_ctx().wrap_socket(sock, server_hostname=config.SPEED_HOST)
        conn.settimeout(5)
        req = (f"GET /__down?bytes=5242880 HTTP/1.1\r\n"
               f"Host: {config.SPEED_HOST}\r\nUser-Agent: Mozilla/5.0 (FastCF)\r\n"
               f"Connection: close\r\n\r\n").encode()
        conn.sendall(req)
        head = _read_head(conn, 5)
    return conn, head


def _parse_head_meta(head: bytes, result: dict):
    """从响应头解析 cf-ray / dc / location（仅 conn_idx==0 调用一次）。"""
    if not head:
        return
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


def _download_worker(ip: str, speed_bytes: int, speed_secs: float,
                      is_cancelled, result: dict, conn_idx: int,
                      total_conns: int, total_bytes: list, total_time: list):
    """单个连接的下载 worker（多线程并发）。

    关键设计：
    - /__down?bytes=N 只服务 N 字节就 EOF；EOF = 文件下载完了（速度快的体现）
    - 速度 = 实际下载大小 / 实际下载时间
    - 4 连接并发，总速度 = 总下载量 / 总时间（取所有连接中最长的实际下载时间）
    """
    conn = None
    try:
        conn, head = _open_conn(ip, speed_bytes, result, conn_idx)
        if conn_idx == 0:
            _parse_head_meta(head, result)

        buf = bytearray(65536)
        conn_start = time.time()
        conn_bytes = 0
        while time.time() - conn_start < speed_secs:
            if is_cancelled and is_cancelled():
                break
            try:
                n = conn.recv_into(buf)
            except socket.timeout:
                continue
            except Exception:
                break
            if not n:
                # EOF：文件下载完了（速度快的体现）→ 记录实际下载时间
                break
            conn_bytes += n
        # 记录本连接的下载量和时间
        actual_time = time.time() - conn_start
        if actual_time > 0 and conn_bytes > 0:
            total_bytes[0] += conn_bytes
            # 总时间 = 所有连接中最长的实际下载时间（并发，所以取 max）
            if actual_time > total_time[0]:
                total_time[0] = actual_time
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _run_once(ip: str, speed_bytes: int, speed_secs: float,
              is_cancelled) -> dict:
    """单次多连接测速（内部函数，供重试调用）。

    速度计算：
    - 4 连接并发下载，总下载量 = 各连接下载量之和
    - 总时间 = 所有连接中最长的实际下载时间（并发，所以取 max）
    - 速度 = 总下载量 / 总时间
    """
    result = {"ip": ip, "port": config.SPEED_PORT, "ping": 0, "mbps": 0,
              "dc": "", "cfRay": "", "location": ""}

    # 共享状态（列表包装，线程间共享）
    total_bytes = [0]
    total_time = [0.0]

    import threading
    threads = []
    for i in range(config.SPEED_CONNS):
        t = threading.Thread(
            target=_download_worker,
            args=(ip, speed_bytes, speed_secs, is_cancelled, result, i,
                  config.SPEED_CONNS, total_bytes, total_time),
            daemon=True
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # 速度 = 总下载量 / 总时间
    if total_time[0] > 0 and total_bytes[0] > 0:
        result["mbps"] = int(total_bytes[0] * 8 / total_time[0] / 1_000_000)
    return result


def speed_test(ip: str, speed_bytes: int, speed_secs: float,
               is_cancelled=None) -> dict:
    """多连接并发 443/TLS 下载测速（绕过 GFW 单连接限速 + 失败重试）。

    返回 {ip, port, ping, mbps, dc, cfRay, location}。
    mbps = 总下载量 / 总时间（所有连接合计）；ping 为第一个连接的 TCP+TLS 时延。
    is_cancelled 为可调用对象，返回 True 时提前结束。
    失败（0 Mbps）时自动重试 SPEED_RETRY 次（间隔 SPEED_RETRY_DELAY 秒）。
    """
    result = _run_once(ip, speed_bytes, speed_secs, is_cancelled)

    # 失败重试（0 Mbps → 重试）
    if result["mbps"] == 0 and config.SPEED_RETRY > 0:
        for attempt in range(config.SPEED_RETRY):
            if is_cancelled and is_cancelled():
                break
            time.sleep(config.SPEED_RETRY_DELAY)
            retry = _run_once(ip, speed_bytes, speed_secs, is_cancelled)
            # 取两次中较好的结果
            if retry["mbps"] > result["mbps"]:
                result = retry
            if result["mbps"] > 0:
                break  # 重试成功，停止

    return result
