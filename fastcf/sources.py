# -*- coding: utf-8 -*-
"""IP 数据源：官方 CF 段 + 外部 443 清单，双源合并随机采样。

- 官方段：cloudflare.com/ips-v4（14 条大段 CIDR），7 天缓存
- 外部清单：zip.cm.edu.kg/all.txt（IP:PORT#CC，仅取 443 端口），7 天缓存
- 采样：两源各取约一半名额，官方侧 /24 分层随机、清单侧直接随机，按 IP 去重
- 持久化统一走 store（原子写 + 单锁）
"""
import ipaddress
import random
import re
import time

from . import config, store
from .net import direct_download


def _parse_cidr_lines(text: str) -> list:
    """解析纯文本 CIDR 列表（逐行，忽略空行与注释，非法行丢弃）。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        out.append(line)
    return out


_EXT_LINE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)(?:#([A-Za-z]{2}))?\s*$")


def parse_ext_lines(text: str) -> tuple:
    """解析外部清单（每行 `IP:PORT#CC`）。

    仅保留 443 端口、合法 IPv4 条目，按出现顺序去重。返回 (ips, kept, skipped)。
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


def _fetch_with_cache(path, url: str, parse, force: bool = False) -> dict:
    """带缓存的源获取（缓存命中直接返回；未命中时**不持锁**下载，
    避免慢下载阻塞其它源的并发获取；下载失败沿用旧缓存，再失败抛错）。"""
    hit = store.cached_source(path, force)
    if hit:
        return hit
    try:
        out = parse(direct_download(url))
        if out.get("v4"):
            store.save_source(path, out)
            return out
    except Exception:
        pass
    stale = store.stale_source(path)
    if stale:
        return stale
    raise RuntimeError(f"数据源获取失败（{url}）")


def fetch_cf_ips(force: bool = False) -> dict:
    """官方 CF IPv4 段。返回 {'v4': [cidr], 'ts': ..., 'source': url}。"""
    def parse(text: str) -> dict:
        v4 = _parse_cidr_lines(text)
        return {"ts": time.time(), "v4": v4, "source": config.CF_IPS_URL}
    return _fetch_with_cache(store.CF_IPS_CACHE, config.CF_IPS_URL, parse, force)


def fetch_external_ips(force: bool = False) -> dict:
    """外部 IP 清单（仅 443 端口、去重后的 IPv4 列表）。"""
    def parse(text: str) -> dict:
        ips, kept, skipped = parse_ext_lines(text)
        return {"ts": time.time(), "v4": ips, "source": config.EXT_IPS_URL,
                "kept": kept, "skipped": skipped}
    return _fetch_with_cache(store.EXT_IPS_CACHE, config.EXT_IPS_URL, parse, force)


def sources_status() -> dict:
    """两源缓存概要（不触网）：供前端数据状态展示。

    health 语义：fresh（TTL 内）/ stale（有旧缓存但已过期）/ missing（无任何缓存）。
    """
    import time as _time
    def _info(path) -> dict:
        d = store.read_json(path, {})
        ts = d.get("ts", 0)
        age = _time.time() - ts if ts else 0
        health = "fresh" if (ts and age <= config.CACHE_TTL) else (
            "stale" if ts else "missing")
        return {"n": len(d.get("v4", [])), "ts": ts,
                "source": d.get("source", ""), "health": health}
    return {"official": _info(store.CF_IPS_CACHE), "external": _info(store.EXT_IPS_CACHE)}


# ── 段归属 / 已知 IP 校验 ──

def is_in_cf_v4(ip: str, cidrs: list = None) -> bool:
    """判断 IPv4 是否落在官方 CF 段内。"""
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
    """已知合法来源：官方 CF 段内 或 外部清单（443 条目）中。"""
    ip = ip.strip()
    if is_in_cf_v4(ip, cidrs):
        return True
    if ext_ips is None:
        try:
            ext_ips = fetch_external_ips().get("v4", [])
        except Exception:
            ext_ips = []
    return ip in set(ext_ips)


# ── 双源合并采样 ──

def _expand_sample(cidrs: list, target: int) -> list:
    """从 CIDR 列表按 /24 前缀分层随机采样，最多取 target×3 个（供上游去重）。"""
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
    cap = target * 3
    while len(ips) < cap:
        random.shuffle(blocks)
        progressed = False
        for net in blocks:
            if len(ips) >= cap:
                break
            hosts = list(net.hosts())
            if not hosts:
                continue
            for h in random.sample(hosts, min(3, len(hosts))):
                s = str(h)
                if s not in ips:
                    ips.append(s)
                    progressed = True
                    if len(ips) >= cap:
                        break
        if not progressed:
            break
    return ips


def sample_cf_ips(count: int) -> list:
    """双源合并随机采样 count 个 IPv4：外部清单约一半 + 官方段（/24 分层）补满。

    按 IP 去重；某源缺失时另一源补满。
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
        rest = [i for i in pool if i not in seen]
        random.shuffle(rest)
        for ip in rest:
            merged.append(ip)
            seen.add(ip)
            if len(merged) >= target:
                break
    random.shuffle(merged)
    return merged[:target]
