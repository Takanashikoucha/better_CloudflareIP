# -*- coding: utf-8 -*-
"""Cloudflare 官方 IP 段获取 / 缓存 / 采样。

数据源：https://www.cloudflare.com/ips-v4 和 ips-v6（纯文本 CIDR 列表）。
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

CF_IPS_URL = {
    "v4": "https://www.cloudflare.com/ips-v4",
    "v6": "https://www.cloudflare.com/ips-v6",
}
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
    """获取并缓存 Cloudflare 官方 IP 段。返回 {'v4': [cidr...], 'v6': [cidr...], 'ts': ...}"""
    if not force and CF_IPS_CACHE.exists():
        try:
            cached = json.loads(CF_IPS_CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_TTL and cached.get("v4") and cached.get("v6"):
                return cached
        except Exception:
            pass
    out = {"ts": time.time(), "v4": [], "v6": []}
    for ver in ("v4", "v6"):
        text = _direct_download(CF_IPS_URL[ver])
        for line in text.splitlines():
            line = line.strip()
            if line:
                out[ver].append(line)
    CF_IPS_CACHE.write_text(json.dumps(out))
    return out


# ── IPv6 前缀工具 ──

def v6_prefixes(net, plen=48):
    """把 IPv6 网段切成 /plen 前缀。"""
    if plen <= net.prefixlen:
        yield net
        return
    step = 1 << (plen - net.prefixlen)
    base = int(net.network_address) & ~((1 << (128 - plen)) - 1)  # 对齐到 plen 边界
    for i in range(step):
        yield ipaddress.ip_network((base + i) << (128 - plen), plen)


def v4_prefixes(net, plen=24):
    if plen <= net.prefixlen:
        yield net
        return
    step = 1 << (plen - net.prefixlen)
    base = int(net.network_address)
    for i in range(step):
        yield ipaddress.ip_network((base + i) << (32 - plen), plen)


# ── 采样 ──

def sample_cf_ips(count: int, use_v6: bool) -> list:
    """从官方 IP 段随机采样 count 个 IP（按 /24、/48 前缀分层随机，不展开全量）。"""
    raw = fetch_cf_ips()
    cidrs = raw["v6"] if use_v6 else raw["v4"]
    return _expand_sample(cidrs, 6 if use_v6 else 4)[:count]


def sample_cf_subnets(count: int, use_v6: bool) -> list:
    """随机采样 count 个 /24（v4）或 /48（v6）子网。

    返回 [str(ip_network), ...]，每个代表一个 CF 官方 CIDR 下的子网。
    用于建池：同一子网内 IP 通常归属同一 DC，探测一个即可批量入池。
    """
    raw = fetch_cf_ips()
    cidrs = raw["v6"] if use_v6 else raw["v4"]
    bit = 32 if not use_v6 else 128
    host = 24 if not use_v6 else 48
    step = 1 << (bit - host)
    mask = step - 1

    blocks = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version != (6 if use_v6 else 4):
            continue
        first = int(net.network_address) & ~mask
        last = int(net.broadcast_address)
        for base in range(first, last + 1, step):
            blocks.append(ipaddress.ip_network((base, host), strict=False))
    random.shuffle(blocks)
    return [str(b) for b in blocks[:count]]


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
    """从 CIDR 列表直接采样（不展开全量）：v4 按 /24、v6 按 /48 前缀各取一个随机主机，
    随机前缀重复取若干轮，保证随机性又避免枚举数百万 IP。"""
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

    # 收集前缀块（每个前缀块是 [first, last] 整数区间）；直接整数运算，避免 ip_network 构造
    bits = 32 if want_ver == 4 else 128
    host_bits = (24 if want_ver == 4 else 48)
    mask = (1 << (bits - host_bits)) - 1
    blocks = []
    for n in nets:
        first = int(n.network_address) & ~mask
        last = int(n.broadcast_address)
        for base in range(first, last + 1, 1 << (bits - host_bits)):
            blocks.append((base, min(base + (1 << (bits - host_bits)) - 1, last)))
    if not blocks:
        return []

    ips = []
    for _ in range(3):  # 3 轮随机前缀
        random.shuffle(blocks)
        for first, last in blocks:
            ips.append(str(ipaddress.ip_address(first + random.getrandbits(bits) % (last - first + 1))))
            if len(ips) >= 5000:
                return ips
    return ips
