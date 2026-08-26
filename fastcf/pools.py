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


def _fill_batch(candidates: list, use_v6: bool, use_tls: bool,
                stop: "callable | None" = None, log=None) -> dict:
    """并发探测一批候选 IP，把每个 IP 写回它实际命中的 DC（无目标过滤）。

    返回 {colo: [ips...]}（本轮入池汇总）。
    """
    import concurrent.futures as cfu

    random.shuffle(candidates)
    by_colo: dict = {}
    lock = threading.Lock()
    done = 0

    def probe(ip):
        ip, colo, err = _probe(ip, use_tls)
        return ip, colo

    with cfu.ThreadPoolExecutor(max_workers=FILL_WORKERS) as ex:
        futs = {ex.submit(probe, ip): ip for ip in candidates}
        for f in cfu.as_completed(futs):
            if stop and stop():
                break
            ip, colo = f.result()
            done += 1
            if done % 10 == 0 and log:
                log(f"后台填充：已探测 {done}/{len(candidates)}")
            if not colo:
                continue
            with lock:
                if size(colo) < POOL_SIZE:
                    add(colo, [ip], save=False)
                by_colo.setdefault(colo.upper(), []).append(ip)
    _save()
    return by_colo


def refill(codes: list | None = None, use_v6: bool = False, use_tls: bool = True,
           max_probes: int = 200, stop: "callable | None" = None, log=None) -> dict:
    """采样官方 IP 段 → 并发探测 → 按实际 DC 入池。

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
        # 先清空（保留数据，重探成功会重新写回；失败的即被丢弃）
        with _lock:
            _pools.clear()
            _pool_ts.clear()
        step = 20
        for i in range(0, min(len(old_ips), max_probes * 3), step):
            if stop and stop():
                break
            batch = _fill_batch(old_ips[i:i + step], use_v6, use_tls, stop, log)
            for k, v in batch.items():
                out.setdefault(k, []).extend(v)
        with _lock:
            if _pools_ts:
                _pools_ts = time.time()
        _save()

    # 2. 缺额采样补池
    if codes is not None:
        need = {c.upper() for c in codes if size(c) < POOL_SIZE}
    else:
        need = {c for c in all_codes() if size(c) < POOL_SIZE}
    if not need:
        return out
    if stop and stop():
        return out

    want = min(1500, max(100, len(need) * 20))
    probes_left = max_probes
    while probes_left > 0 and (need and any(size(c) < POOL_SIZE for c in need)):
        if stop and stop():
            break
        n = min(probes_left, 20)
        candidates = ipdata.sample_cf_ips(n, use_v6)
        batch = _fill_batch(candidates, use_v6, use_tls, stop, log)
        probes_left -= n
        for k, v in batch.items():
            out.setdefault(k, []).extend(v)
            need.discard(k.upper())
        if need:
            time.sleep(FILL_PAUSE)
    return out
