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
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
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
    base = int(net.network_address)
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

def sample_cf_ips(count: int, use_v6: bool, geo_db=None, country_filter: list | None = None):
    """
    从官方 IP 段采样 count 个 IP。

    若提供 geo_db（geoip.Ip2RegionDB）且 country_filter 非空：
      按 /48（v6）或 /24（v4）前缀探测实际归属国，只保留属于目标国家的 IP；
      归属信息按 IP 真实归属解析（ip2region），命中前缀内全部主机 IP 都可用。
    否则：全量随机采样。

    返回 (ips, matched_prefixes, total_prefixes, fallback: bool)
      fallback=True 表示地理过滤未命中、已回退全量随机。
    """
    raw = fetch_cf_ips()
    cidrs = raw["v6"] if use_v6 else raw["v4"]
    want_ver = 6 if use_v6 else 4

    use_geo = geo_db is not None and bool(country_filter)
    if use_geo:
        prefixes = []
        seen = set()
        for c in cidrs:
            try:
                net = ipaddress.ip_network(c, strict=False)
            except ValueError:
                continue
            if net.version != want_ver:
                continue
            gen = v6_prefixes(net, 48) if want_ver == 6 else v4_prefixes(net, 24)
            for p in gen:
                key = int(p.network_address)
                if key not in seen:
                    seen.add(key)
                    prefixes.append(p)
        random.shuffle(prefixes)

        matched_ips = []
        matched_prefixes = 0
        need = count * 4  # 多备一些，测速淘汰后仍够
        for p in prefixes:
            probe = str(p.network_address + (1 if p.network_address < p.broadcast_address else 0))
            cc = geo_db.country_code(probe)
            if cc in {c.upper() for c in country_filter}:
                matched_prefixes += 1
                for h in p.hosts():
                    matched_ips.append(str(h))
                    if len(matched_ips) >= need:
                        break
            if len(matched_ips) >= need:
                break

        if matched_ips:
            random.shuffle(matched_ips)
            return matched_ips[:count], matched_prefixes, len(prefixes), False
        # 未命中：回退全量
        all_ips = _expand_all(cidrs, want_ver)
        random.shuffle(all_ips)
        return all_ips[:count], 0, len(prefixes), True

    all_ips = _expand_all(cidrs, want_ver)
    random.shuffle(all_ips)
    return all_ips[:count], 0, len(cidrs), False


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


def _expand_all(cidrs, want_ver):
    """展开全部 CIDR 为 IP 列表（v6 按 /48 每个取一个随机主机）。"""
    ips = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version != want_ver:
            continue
        if want_ver == 4:
            for h in net.hosts():
                ips.append(str(h))
        else:
            for p in v6_prefixes(net, 48):
                first = int(p.network_address)
                last = int(p.broadcast_address)
                if last > first:
                    idx = random.getrandbits(64) % (last - first + 1)
                    ips.append(str(ipaddress.ip_address(first + idx)))
    return ips
