# -*- coding: utf-8 -*-
"""DC 级 IP 池。

每个 DC（colo）维护约 POOL_SIZE 个已验证可直连的 IP；
扫描时从池里随机取 IP 测速，任何探测行为只要读到实际服务节点
（cf-meta-colo），就把 IP 写回**实际命中的那个 DC**（无目标过滤）。

TTL 语义：池按"最后入池时间"过期（默认 7 天）。过期后 IP **不删除**，
由后台 filler 重新探测：探测成功 → 写回实际 DC 池并刷新时间戳；
探测失败 → 剔除。
"""
import ipaddress
import json
import os
import random
import threading
import time
from pathlib import Path

from . import geoip, ipdata

DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
POOL_FILE = DATA_DIR / "ip_pools.json"

POOL_SIZE = 50       # 每个 DC 目标的 IP 数量
TEST_SIZE = 50       # 每次扫描从池里随机测的 IP 数量
TTL = 7 * 86400      # 池有效期（过期触发重新探测，不直接删除）
FILL_WORKERS = 4     # 后台填充并发数（温和，避免触发 CF 限流）
FILL_PAUSE = 0.3     # 后台填充探测间隔（秒）

_pools: dict | None = None
_pool_ts: dict = {}          # {DC: 该 DC 池最后刷新时间}
_pools_ts: float = 0         # 整体最后刷新时间
_lock = threading.Lock()


def _load():
    global _pools, _pools_ts, _pool_ts
    try:
        d = json.loads(POOL_FILE.read_text())
        _pools = {k: v["ips"] for k, v in d.get("pools", {}).items() if v.get("ips")}
        _pool_ts = {k: v.get("ts", 0) for k, v in d.get("pools", {}).items()}
        _pools_ts = d.get("ts", 0)
    except Exception:
        _pools, _pool_ts, _pools_ts = {}, {}, 0


def _save():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        POOL_FILE.write_text(json.dumps(
            {"ts": time.time(),
             "pools": {k: {"ips": v, "ts": _pool_ts.get(k, 0)} for k, v in (_pools or {}).items()}}))
    except Exception:
        pass


def _ensure_loaded():
    global _pools
    if _pools is None:
        with _lock:
            if _pools is None:
                _load()


def get(code: str) -> list:
    """取某 DC 的 IP 池（可能为空）。"""
    _ensure_loaded()
    return list((_pools or {}).get(code.upper(), []))


def size(code: str) -> int:
    return len(get(code))


def add(code: str, ips: list, max_size: int = POOL_SIZE, save: bool = True):
    """把验证成功的 IP 并入指定 DC 的池（去重、保持顺序、截断）。"""
    _ensure_loaded()
    with _lock:
        pool = _pools.setdefault(code.upper(), [])
        changed = False
        for ip in ips:
            if ip and ip not in pool:
                pool.append(ip)
                changed = True
        if len(pool) > max_size:
            pool[:] = pool[-max_size:]
            changed = True
        if changed:
            _pool_ts[code.upper()] = time.time()
            _pools_ts = time.time()
        if changed and save:
            _save()


def remove(code: str, ips: list):
    """从指定 DC 的池中剔除 IP。"""
    _ensure_loaded()
    with _lock:
        pool = _pools.get(code.upper())
        if not pool:
            return
        before = len(pool)
        pool[:] = [ip for ip in pool if ip not in set(ips)]
        if len(pool) < before:
            _save()


def expired() -> bool:
    """池整体是否超过 TTL（过期触发重新探测）。"""
    return bool(_pools_ts) and time.time() - _pools_ts > TTL


def all_codes() -> list:
    _ensure_loaded()
    return list((_pools or {}).keys())


def pool_report() -> dict:
    """池统计：{code: n}。"""
    _ensure_loaded()
    return {c: len(v) for c, v in (_pools or {}).items() if v}


def pools_detail() -> list:
    """池明细（前端面板）：[{code, cc, cc_zh, size, ips}]，按 size 降序。"""
    rep = pool_report()
    out = []
    for code, n in rep.items():
        cc = geoip.colo_country(code) or ""
        out.append({
            "code": code,
            "cc": cc,
            "cc_zh": geoip.country_zh(cc) if cc else "",
            "size": n,
            "ips": list((_pools or {}).get(code, [])),
        })
    out.sort(key=lambda x: (-x["size"], x["code"]))
    return out


def clear_pool(code: str) -> int:
    """清空指定 DC 的池，返回被删 IP 数。"""
    _ensure_loaded()
    with _lock:
        n = len(_pools.get(code.upper(), []))
        _pools.pop(code.upper(), None)
        _pool_ts.pop(code.upper(), None)
        if n:
            _save()
    return n


def clear_all() -> int:
    """清空全部池，返回总 IP 数。"""
    global _pools, _pool_ts, _pools_ts
    _ensure_loaded()
    with _lock:
        n = sum(len(v) for v in (_pools or {}).values())
        _pools = {}
        _pool_ts = {}
        _pools_ts = 0
        if n:
            _save()
    return n


def _probe(ip, use_tls, timeout=4):
    """探测单个 IP 的实际服务节点。返回 (ip, colo, err)。"""
    try:
        _cc, colo, _city = ipdata.probe_location(ip, use_tls, timeout=timeout)
        return ip, colo, None
    except Exception as e:
        return ip, None, str(e)


def probe_and_add(ips: list, code_hint: str = "", use_v6: bool = False,
                  use_tls: bool = True, workers: int = 12, log=None) -> dict:
    """手动补充 IP 入池：CF 官方 IP 段校验 → 并发探测实际 colo → 按实际 DC 归池。

    code_hint 非空时，探测结果与 hint 不符的计入 mismatch、不入库。
    """
    import concurrent.futures as cfu

    cf_cache = ipdata.fetch_cf_ips()
    nets = []
    for ver in ("v4", "v6"):
        for c in cf_cache.get(ver, []):
            try:
                nets.append(ipaddress.ip_network(c, strict=False))
            except Exception:
                pass

    def in_cf(ip_str: str):
        try:
            a = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(n.version == a.version and a in n for n in nets)

    details = []
    pending = []
    for raw in ips:
        ip = raw.strip()
        if not ip:
            continue
        if not in_cf(ip):
            details.append({"ip": ip, "ok": False, "reason": "不在 CF 官方 IP 段"})
            continue
        pending.append(ip)

    by_colo: dict = {}
    failed = 0
    mismatch = 0
    target = (code_hint or "").upper()

    def probe(ip):
        ip, colo, err = _probe(ip, use_tls)
        if err:
            return ip, None, f"探测异常：{err}"
        if not colo:
            return ip, None, "探测无响应/无 colo 头"
        if target and colo.upper() != target:
            return ip, colo, f"实际 colo={colo} 与目标 {target} 不符"
        return ip, colo, None

    with cfu.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, ip): ip for ip in pending}
        for f in cfu.as_completed(futs):
            ip, colo, reason = f.result()
            if log:
                log(f"探测 {ip} → {colo or '(失败)'}")
            if colo:
                by_colo.setdefault(colo.upper(), []).append(ip)
            else:
                if "不符" in (reason or ""):
                    mismatch += 1
                else:
                    failed += 1
                details.append({"ip": ip, "ok": False, "reason": reason or "探测失败"})

    added = 0
    for colo, ip_list in by_colo.items():
        before = size(colo)
        add(colo, ip_list)
        delta = size(colo) - before
        added += delta
        if log:
            log(f"  → 入池 {colo}：+{delta}")

    return {
        "added": added,
        "rejected": len(ips) - len(pending),
        "mismatch": mismatch,
        "failed": failed,
        "by_colo": by_colo,
        "details": details,
    }


def _fill_subnet_batch(subnets: list, use_v6: bool, use_tls: bool,
                       ips_per_subnet: int = 10,
                       stop: "callable | None" = None, log=None) -> dict:
    """按子网（/24 或 /48）并发探测，探测成功后批量加入同子网的邻居 IP。

    同一子网内 CF 通常把连续 IP 分配给同一边缘节点，所以探测一个 IP
    确认 DC 后，可以直接把同子网的其余 IP 批量入池（它们大概率同 DC）。
    返回 {colo: [ips...]}（本轮入池汇总）。
    """
    import concurrent.futures as cfu

    random.shuffle(subnets)
    by_colo: dict = {}
    lock = threading.Lock()
    done = 0
    total = len(subnets)

    def probe_subnet(subnet_str):
        """探测一个子网：取一个 IP 探测 → 成功则随机取同子网的邻居 IP 批量返回。"""
        try:
            net = ipaddress.ip_network(subnet_str, strict=False)
        except Exception:
            return None
        hosts = list(net.hosts())
        if not hosts:
            return None
        test_ip = str(random.choice(hosts))
        _ip, colo, err = _probe(test_ip, use_tls)
        if not colo:
            return None
        # 探测成功：随机取同子网的 IP（避免顺序取导致集中在不可达区间）
        n = min(ips_per_subnet, len(hosts))
        ips = [str(h) for h in random.sample(hosts, n)]
        return subnet_str, colo, ips

    with cfu.ThreadPoolExecutor(max_workers=FILL_WORKERS) as ex:
        futs = {ex.submit(probe_subnet, s): s for s in subnets}
        for f in cfu.as_completed(futs):
            if stop and stop():
                break
            r = f.result()
            done += 1
            if done % 5 == 0 and log:
                log(f"后台填充：已探测 {done}/{total} 个子网")
            if not r:
                continue
            _subnet, colo, ips = r
            with lock:
                if size(colo) < POOL_SIZE:
                    add(colo, ips, save=False)
                by_colo.setdefault(colo.upper(), []).extend(ips)
    _save()
    return by_colo


def refill(codes: list | None = None, use_v6: bool = False, use_tls: bool = True,
           max_probes: int = 200, stop: "callable | None" = None, log=None) -> dict:
    """按子网采样官方 IP 段 → 并发探测 → 按实际 DC 批量入池。

    利用 CF 同一 /24（v4）或 /48（v6）子网内 IP 通常归属同一 DC 的特性，
    每个子网只探测一个 IP，成功后把同子网的邻居 IP 批量入池，
    大幅减少探测次数。

    codes 仅用于判断缺额（None = 全部有池的 DC 都算）；
    探测命中的任何 DC 都会入池（含 codes 之外的 DC）。
    池整体过期时，先把过期池里的 IP 重新探测一遍（成功刷新、失败剔除），
    再对缺额采样补池。
    """
    _ensure_loaded()
    out: dict = {}

    # 1. 过期重探：池里的老 IP 逐个探测，成功刷新实际 DC、失败剔除
    if expired():
        old_ips: list = []
        for c in all_codes():
            old_ips.extend(get(c))
        if old_ips and log:
            log(f"池已过期，重新探测 {len(old_ips)} 个旧 IP…")
        with _lock:
            _pools.clear()
            _pool_ts.clear()
        # 旧 IP 按 /24 分组，每组只探测一个
        by_subnet: dict = {}
        for ip in old_ips:
            try:
                a = ipaddress.ip_address(ip)
            except Exception:
                continue
            if a.version == 4:
                key = str(ipaddress.ip_network((int(a) & 0xFFFFFFF0, 24), strict=False))
            else:
                key = str(ipaddress.ip_network((int(a) & ~((1 << 80) - 1), 48), strict=False))
            by_subnet.setdefault(key, []).append(ip)
        subnet_list = list(by_subnet.keys())
        random.shuffle(subnet_list)
        max_subnets = min(len(subnet_list), max_probes)
        done = 0
        for subnet_str in subnet_list[:max_subnets]:
            if stop and stop():
                break
            group = by_subnet[subnet_str]
            test_ip = random.choice(group)
            _ip, colo, err = _probe(test_ip, use_tls)
            done += 1
            if done % 20 == 0 and log:
                log(f"重探：{done}/{max_subnets} 个子网")
            if colo:
                with _lock:
                    if size(colo) < POOL_SIZE:
                        add(colo, group, save=False)
                out.setdefault(colo.upper(), []).extend(group)
        with _lock:
            if _pools_ts:
                _pools_ts = time.time()
        _save()

    # 2. 缺额采样补池（按子网）
    if codes is not None:
        need = {c.upper() for c in codes if size(c) < POOL_SIZE}
    else:
        need = {c for c in all_codes() if size(c) < POOL_SIZE}
    if not need:
        return out
    if stop and stop():
        return out

    # 每个子网探测 1 个 IP、批量加入 ~10 个邻居，
    # max_probes 个探测 ≈ max_probes*10 个 IP 入池
    probes_left = max_probes
    while probes_left > 0 and (need and any(size(c) < POOL_SIZE for c in need)):
        if stop and stop():
            break
        n = min(probes_left, 20)
        subnets = ipdata.sample_cf_subnets(n, use_v6)
        batch = _fill_subnet_batch(subnets, use_v6, use_tls, stop=stop, log=log)
        probes_left -= n
        for k, v in batch.items():
            out.setdefault(k, []).extend(v)
            need.discard(k.upper())
        if need:
            time.sleep(FILL_PAUSE)
    return out
