# -*- coding: utf-8 -*-
"""Cloudflare IPv4 段获取 / 缓存 / 采样。

数据源（主）：[TYOYO1/CF-ASN](https://github.com/TYOYO1/CF-ASN) 的
cf-asn-list.txt（AS13335 + AS209242 全量 CIDR，约 877 条，含大量 /23、/24，
覆盖官方 ips-v4 之外散布的各 DC 子网，随机采样空间更大）。
数据源（备）：https://www.cloudflare.com/ips-v4（14 条大段，主源失败时兜底）。
缓存 7 天，过期自动刷新；主源失败自动回退官方源。
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

CF_IPS_URLS = [
    "https://raw.githubusercontent.com/TYOYO1/CF-ASN/main/cf-asn-list.txt",  # 主源：全量 ASN 段
    "https://www.cloudflare.com/ips-v4",                                    # 备源：官方段
]
CACHE_TTL = 7 * 86400  # 7 天


def _direct_download(url, timeout=30, retries=3):
    """绕过代理直连下载（socket 级超时，防止慢速连接挂死）。

    网络偶发中断（TLS EOF / 读超时）时重试：最多 retries 次，
    退避 1s、2s；全部失败才抛异常。
    """
    import socket
    import time as _time
    import urllib.request
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as r:
                r.fp.raw._sock.settimeout(timeout)  # socket 读超时
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                _time.sleep(1 + attempt)
    raise last_err


def _parse_cidr_lines(text: str) -> list:
    """解析纯文本 CIDR 列表（逐行，忽略空行与注释）。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ipaddress.ip_network(line, strict=False)  # 校验，非法行丢弃
        except ValueError:
            continue
        out.append(line)
    return out


def fetch_cf_ips(force=False) -> dict:
    """获取并缓存 Cloudflare IPv4 段。返回 {'v4': [cidr...], 'ts': ..., 'source': url}

    按 CF_IPS_URLS 顺序尝试（主源 TYOYO1/CF-ASN，备源官方 ips-v4），全部失败抛异常。
    """
    if not force and CF_IPS_CACHE.exists():
        try:
            cached = json.loads(CF_IPS_CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_TTL and cached.get("v4"):
                return cached
        except Exception:
            pass
    last_err = None
    for url in CF_IPS_URLS:
        try:
            v4 = _parse_cidr_lines(_direct_download(url))
            if not v4:
                last_err = f"{url} 返回空列表"
                continue
            out = {"ts": time.time(), "v4": v4, "source": url}
            try:
                # 原子写：先写临时文件再 rename，避免中途失败留下截断/陈旧缓存
                # （非原子写失败过：内存是 877 条、磁盘却停在 15 条，界面一直显示 15）
                tmp = CF_IPS_CACHE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(out))
                tmp.replace(CF_IPS_CACHE)
            except Exception:
                pass  # 落盘失败不影响本次返回
            return out
        except Exception as e:
            last_err = f"{url}: {e}"
    raise RuntimeError(f"所有 CF IPv4 段数据源均获取失败：{last_err}")


# ── 段归属校验 ──

def is_in_cf_v4(ip: str, cidrs: list = None) -> bool:
    """判断 IPv4 是否落在 CF 段内（cidrs 缺省用缓存/现取的官方段）。"""
    if cidrs is None:
        cidrs = fetch_cf_ips().get("v4", [])
    try:
        a = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if a.version != 4:
        return False
    for c in cidrs:
        try:
            if a in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


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


# ── 段首 IP 探测（池初始化）──

def first_ip_per_segment(cidrs: list = None) -> list:
    """每个 CF IPv4 段取首个可用主机 IP（.0 网络地址与 /31、/32 跳过）。

    cidrs 缺省用缓存/现取的段列表。TYOYO1/CF-ASN 全量段约 877 个 → 约 877 个首 IP。
    /31 段跳过（CPython 3.9+ 的 hosts() 把两端地址都当可用主机，/31 首地址是网络地址，
    不适合探测）；/32 单主机段保留。
    """
    if cidrs is None:
        cidrs = fetch_cf_ips().get("v4", [])
    out = []
    seen = set()
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.prefixlen == 31:
            continue  # /31 不可靠（两端地址角色因实现而异），不取
        hosts = list(net.hosts())
        if not hosts:
            continue  # 理论不可达（/31 已提前跳过），防御性保留
        first = str(hosts[0])
        if first not in seen:
            seen.add(first)
            out.append(first)
    return out


def segment_first_ips_probe(workers: int = 20, log=None) -> dict:
    """池初始化：对每个 CF IPv4 段的首个 IP 并发探测实际服务节点（cf-meta-colo）并入池。

    复用 pools.probe_and_add（首 IP 必在 CF 段内，段校验恒通过）。
    返回 probe_and_add 的结果 + 探测的 IP 总数。
    """
    from . import pools  # 延迟导入避免循环依赖

    ips = first_ip_per_segment()
    if log:
        log(f"段首 IP 探测：{len(ips)} 个（每段首个 IP，并发 {workers}）")
    res = pools.probe_and_add(ips, "", use_tls=True, workers=workers, log=log)
    res["total"] = len(ips)
    return res


# ── 采样 ──

def sample_cf_ips(count: int, use_v6: bool = False) -> list:
    """从 CF IPv4 段（TYOYO1/CF-ASN 全量段，主源失败回退官方 ips-v4）随机采样 count 个 IP（按 /24 前缀分层随机，不展开全量）。

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
