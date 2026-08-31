# -*- coding: utf-8 -*-
"""Cloudflare IPv4 IP 来源：官方段 + 外部清单，双源合并采样。

数据源（双源，50/50 合并随机）：
  1. 官方段：https://www.cloudflare.com/ips-v4（14 条大段，CIDR 列表）
     —— 缓存 ~/.fastcf/cf_ips.json，7 天 TTL
  2. 外部清单：https://zip.cm.edu.kg/all.txt（约 1.7 万条 IP:PORT#国家 标签，
     仅保留 443 端口条目，去重后为纯 IPv4 列表）
     —— 缓存 ~/.fastcf/ext_ips.json，7 天 TTL；拉取失败退化为仅官方源
采样：sample_cf_ips(count) 两源各取约 count/2，官方侧 /24 分层随机、
清单侧直接随机，按 IP 去重。
"""
import ipaddress
import json
import os
import random
import re
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CF_IPS_CACHE = DATA_DIR / "cf_ips.json"
EXT_IPS_CACHE = DATA_DIR / "ext_ips.json"

CF_IPS_URL = "https://www.cloudflare.com/ips-v4"     # 官方段（CIDR）
EXT_IPS_URL = "https://zip.cm.edu.kg/all.txt"        # 外部 IP 清单（IP:PORT#CC）
CACHE_TTL = 7 * 86400  # 两源缓存均为 7 天


def _direct_download(url, timeout=30, retries=3):
    """绕过代理直连下载（socket 级超时，防止慢速连接挂死）。

    网络偶发中断（TLS EOF / 读超时）时重试：最多 retries 次，
    退避 1s、2s；全部失败才抛异常。
    """
    import time as _time
    import urllib.request
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                _time.sleep(1 + attempt)
    raise last_err


def _atomic_write(path: Path, obj: dict):
    """原子落盘（临时文件 + rename），避免中途失败留下截断/陈旧缓存。"""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(path)
    except Exception:
        pass  # 落盘失败不影响本次返回


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


_EXT_LINE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)(?:#([A-Za-z]{2}))?\s*$")


def parse_ext_lines(text: str) -> tuple:
    """解析外部清单（每行 `IP:PORT#CC`）。

    仅保留 443 端口、合法 IPv4 条目（其余端口与非法行静默跳过），
    按出现顺序去重。返回 (ips, kept, skipped)。
    """
    ips, seen, kept, skipped = [], set(), 0, 0
    for line in text.splitlines():
        m = _EXT_LINE.match(line.strip())
        if not m:
            skipped += 1
            continue
        ip_s, port = m.group(1), m.group(2)
        if int(port) != 443:
            skipped += 1
            continue
        try:
            a = ipaddress.ip_address(ip_s)
        except ValueError:
            skipped += 1
            continue
        if a.version != 4 or ip_s in seen:
            skipped += 1
            continue
        seen.add(ip_s)
        ips.append(ip_s)
        kept += 1
    return ips, kept, skipped


def fetch_cf_ips(force=False) -> dict:
    """获取并缓存官方 CF IPv4 段。返回 {'v4': [cidr...], 'ts': ..., 'source': url}。

    缓存 7 天内直接复用；过期或 force 时重新下载，失败沿用旧缓存。
    """
    if not force and CF_IPS_CACHE.exists():
        try:
            cached = json.loads(CF_IPS_CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_TTL and cached.get("v4"):
                return cached
        except Exception:
            pass
    try:
        v4 = _parse_cidr_lines(_direct_download(CF_IPS_URL))
        if v4:
            out = {"ts": time.time(), "v4": v4, "source": CF_IPS_URL}
            _atomic_write(CF_IPS_CACHE, out)
            return out
    except Exception:
        pass
    # 下载失败：沿用旧缓存（哪怕过期），彻底没有才抛异常
    if CF_IPS_CACHE.exists():
        try:
            cached = json.loads(CF_IPS_CACHE.read_text())
            if cached.get("v4"):
                return cached
        except Exception:
            pass
    raise RuntimeError(f"官方 CF IPv4 段获取失败（{CF_IPS_URL}）")


def fetch_external_ips(force=False) -> dict:
    """获取并缓存外部 IP 清单（仅 443 端口、去重后的 IPv4 列表）。

    返回 {'v4': [ip...], 'ts': ..., 'source': url, 'kept': n, 'skipped': n}。
    缓存 7 天内直接复用；过期或 force 时重新下载，失败沿用旧缓存。
    """
    if not force and EXT_IPS_CACHE.exists():
        try:
            cached = json.loads(EXT_IPS_CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_TTL and cached.get("v4"):
                return cached
        except Exception:
            pass
    try:
        ips, kept, skipped = parse_ext_lines(_direct_download(EXT_IPS_URL))
        if ips:
            out = {"ts": time.time(), "v4": ips, "source": EXT_IPS_URL,
                   "kept": kept, "skipped": skipped}
            _atomic_write(EXT_IPS_CACHE, out)
            return out
    except Exception:
        pass
    if EXT_IPS_CACHE.exists():
        try:
            cached = json.loads(EXT_IPS_CACHE.read_text())
            if cached.get("v4"):
                return cached
        except Exception:
            pass
    raise RuntimeError(f"外部 IP 清单获取失败（{EXT_IPS_URL}）")


def sources_status() -> dict:
    """两源缓存概要（不触网）：供前端数据状态展示。"""
    def _info(path: Path) -> dict:
        try:
            d = json.loads(path.read_text())
            return {"n": len(d.get("v4", [])), "ts": d.get("ts", 0),
                    "source": d.get("source", "")}
        except Exception:
            return {"n": 0, "ts": 0, "source": ""}
    return {"official": _info(CF_IPS_CACHE), "external": _info(EXT_IPS_CACHE)}


# ── 段归属 / 已知 IP 校验 ──

def is_in_cf_v4(ip: str, cidrs: list = None) -> bool:
    """判断 IPv4 是否落在官方 CF 段内（cidrs 缺省用缓存/现取的官方段）。"""
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


def is_known_ip(ip: str, cidrs: list = None, ext_ips: list = None) -> bool:
    """判断 IP 是否为已知合法来源：官方 CF 段内 或 外部清单（443 条目）中。

    供手动入池校验使用；cidrs/ext_ips 缺省时取缓存（不触网，缓存缺失才现取）。
    """
    ip = ip.strip()
    if is_in_cf_v4(ip, cidrs):
        return True
    if ext_ips is None:
        try:
            ext_ips = fetch_external_ips().get("v4", [])
        except Exception:
            ext_ips = []
    return ip in set(ext_ips)


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
    """每个官方 CF IPv4 段取首个可用主机 IP（.0 网络地址与 /31、/32 跳过）。

    cidrs 缺省用缓存/现取的段列表（官方段仅 14 条 → 约 14 个首 IP）。
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
    """池初始化：对每个官方 CF IPv4 段的首个 IP 并发探测实际服务节点（cf-meta-colo）并入池。

    复用 pools.probe_and_add（首 IP 必在 CF 段内，段校验恒通过）。
    返回 probe_and_add 的结果 + 探测的 IP 总数。
    """
    from . import pools  # 延迟导入避免循环依赖

    ips = first_ip_per_segment()
    if log:
        log(f"段首 IP 探测：{len(ips)} 个（官方段每段首个 IP，并发 {workers}）")
    res = pools.probe_and_add(ips, "", use_tls=True, workers=workers, log=log)
    res["total"] = len(ips)
    return res


# ── 双源合并采样 ──

def _expand_sample(cidrs, target: int):
    """从 CIDR 列表按 /24 前缀分层随机采样，最多取 target 个（不展开全量）。

    每个 /24 块内随机取 1–3 个主机，多轮 shuffle 遍历，够数即停。
    """
    if target <= 0:
        return []
    nets = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            nets.append(net)
    if not nets:
        return []

    blocks = []
    for n in nets:
        if n.prefixlen > 24:
            blocks.append(n)
        elif n.prefixlen < 24:
            blocks.extend(n.subnets(new_prefix=24))
        else:
            blocks.append(n)
    if not blocks:
        return []

    ips = []
    while len(ips) < target * 3:  # 多采一点供上游去重，上限 target*3
        random.shuffle(blocks)
        progressed = False
        for net in blocks:
            hosts = list(net.hosts())
            if not hosts:
                continue
            take = min(3, len(hosts))
            for h in random.sample(hosts, take):
                s = str(h)
                if s not in ips:
                    ips.append(s)
                    progressed = True
                    if len(ips) >= target * 3:
                        break
        if not progressed:
            break
    return ips


def sample_cf_ips(count: int, use_v6: bool = False) -> list:
    """双源合并随机采样 count 个 IPv4：外部清单（443 条目）约一半 + 官方段（/24 分层）补满。

    按 IP 去重（清单 IP 理论上可能落在官方段内）；某源缺失时另一源补满。
    use_v6 参数保留仅为向后兼容旧调用，v6 已不支持，忽略。
    """
    target = max(1, int(count))
    half = (target + 1) // 2

    off, ext, pool = [], [], []
    try:
        pool = fetch_external_ips().get("v4", [])
        ext = random.sample(pool, min(half, len(pool))) if pool else []
    except Exception:
        ext = []
    try:
        off = _expand_sample(fetch_cf_ips().get("v4", []), target)
    except Exception:
        off = []

    # 合并去重：先外部清单（配额小、保证全部入选），再官方分层采样补满
    merged, seen = [], set()
    for ip in ext:
        if ip not in seen:
            merged.append(ip)
            seen.add(ip)
    for ip in off:
        if ip not in seen:
            merged.append(ip)
            seen.add(ip)
        if len(merged) >= target:
            break
    if len(merged) < target:
        # 官方分层块不足以补满（如段极小）→ 从清单剩余 IP 中补
        rest = [i for i in pool if i not in seen]
        random.shuffle(rest)
        for ip in rest:
            merged.append(ip)
            seen.add(ip)
            if len(merged) >= target:
                break
    random.shuffle(merged)
    return merged[:target]


def probe_location(ip: str, use_tls=True, timeout=4):
    """
    对单个 IP 发一个极小流量请求（64KB，读头即断连），读 CF 返回的**实际服务地**。

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
            ctx = _ssl_ctx()
            conn = ctx.wrap_socket(sock, server_hostname=host)
        else:
            conn = sock
        conn.settimeout(timeout + 4)
        req = (f"GET /__down?bytes=65536 HTTP/1.1\r\n"
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


_SSL_CTX = None


def _ssl_ctx():
    """进程级单例 SSL 上下文（跳过主机名校验/证书校验，仅用于读响应头）。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX = ctx
    return _SSL_CTX
