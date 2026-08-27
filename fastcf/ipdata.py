# -*- coding: utf-8 -*-
"""Cloudflare 官方 IPv4 段获取 / 缓存 / 采样。

数据源：https://www.cloudflare.com/ips-v4（纯文本 CIDR 列表，IPv6 已移除）。
缓存 7 天，过期自动刷新。
"""
import ipaddress
import json
import os
import random
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CF_IPS_CACHE = DATA_DIR / "cf_ips.json"

CF_IPS_URL = "https://www.cloudflare.com/ips-v4"
CACHE_TTL = 7 * 86400  # 7 天


def _direct_download(url, timeout=30):
    """绕过代理直连下载（socket 级超时，防止慢速连接挂死）。"""
    import socket
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        r.fp.raw._sock.settimeout(timeout)  # socket 读超时
        return r.read().decode("utf-8", "ignore")


def fetch_cf_ips(force=False) -> dict:
    """获取并缓存 Cloudflare 官方 IPv4 段。返回 {'v4': [cidr...], 'ts': ...}"""
    if not force and CF_IPS_CACHE.exists():
        try:
            cached = json.loads(CF_IPS_CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_TTL and cached.get("v4"):
                return cached
        except Exception:
            pass
    text = _direct_download(CF_IPS_URL)
    out = {"ts": time.time(), "v4": [line.strip() for line in text.splitlines() if line.strip()]}
    CF_IPS_CACHE.write_text(json.dumps(out))
    return out


# ── IPv4 前缀工具 ──

def v4_prefixes(net, plen=24):
    """把 IPv4 网段切成 /plen 前缀（如 /8 → 65536 个 /24）。

    实现方式同 v6_prefixes。
    """
    if net.version != 4:
        raise ValueError(f"v4_prefixes 只接受 IPv4 网段：{net}")
    if plen <= net.prefixlen:
        yield net
        return
    for prefix in net.subnets(new_prefix=plen):
        yield prefix


# ── 采样 ──

def sample_cf_ips(count: int, use_v6: bool = False) -> list:
    """从官方 IPv4 段随机采样 count 个 IP（按 /24 前缀分层随机，不展开全量）。

    use_v6 参数保留仅为向后兼容旧调用，v6 已不支持，忽略。
    """
    raw = fetch_cf_ips()
    return _expand_sample(raw["v4"], 4)[:count]


def probe_location(ip: str, use_tls=True, timeout=4):
    """
    对单个 IP 发一个极小流量请求（1MB），读 CF 返回的**实际服务地**。

    CF 的 IP 是全球共享池，注册归属地 ≠ 实际服务地。
    speed.cloudflare.com/__down 响应头里的 cf-meta-country / city / colo
    返回的就是该 IP 这次实际从哪个边缘节点服务。

    返回 (country_code, colo, city)，失败返回 (None, None, None)。
    """
    import socket
    import ssl
    host = "speed.cloudflare.com"
    port = 443 if use_tls else 80
    conn = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = ctx.wrap_socket(sock, server_hostname=host)
        else:
            conn = sock
        conn.settimeout(timeout + 4)
        req = (f"GET /__down?bytes=1048576 HTTP/1.1\r\n"
               f"Host: {host}\r\nUser-Agent: Mozilla/5.0 (FastCF)\r\n"
               f"Connection: close\r\n\r\n").encode()
        conn.sendall(req)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                break
            head += chunk
        if b"\r\n\r\n" not in head:
            return (None, None, None)
        meta = {}
        for line in head.split(b"\r\n\r\n", 1)[0].decode("latin-1", "ignore").split("\r\n"):
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
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


def _expand_sample(cidrs, want_ver):
    """从 CIDR 列表直接采样（不展开全量）：v4 按 /24、v6 按 /48 前缀分层，
    每个前缀块内随机取主机；随机前缀重复取若干轮，保证随机性又避免枚举数百万 IP。

    用 ipaddress 的 .hosts() 生成器取随机主机（C 层字符串操作，不用整数算术）。
    """
    nets = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version == want_ver:
            nets.append(net)
    if not nets:
        return []

    # 按目标子网分层
    target_plen = 24 if want_ver == 4 else 48
    blocks = []
    for n in nets:
        if n.prefixlen > target_plen:
            blocks.append(n)
        elif n.prefixlen < target_plen:
            blocks.extend(n.subnets(new_prefix=target_plen))
        else:
            blocks.append(n)
    if not blocks:
        return []

    # 每块随机取 1-3 个主机，重复 3 轮
    ips = []
    for _ in range(3):
        random.shuffle(blocks)
        for net in blocks:
            hosts = list(net.hosts())
            if not hosts:
                continue
            take = min(3, len(hosts))
            for h in random.sample(hosts, take):
                ips.append(str(h))
                if len(ips) >= 5000:
                    return ips
    return ips
