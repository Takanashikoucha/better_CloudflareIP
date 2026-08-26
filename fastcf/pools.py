# -*- coding: utf-8 -*-
"""DC 级 IP 池缓存。

每个 DC（colo）维护约 POOL_SIZE 个已验证可直连的 IP 作为基底池；
扫描时从池里随机取 TEST_SIZE 个测速，测速结果（新验证的 IP）回写池。
冷启动（池为空）时扫描回退到全量采样。
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

POOL_SIZE = 100    # 每个 DC 缓存的 IP 数量
TEST_SIZE = 100    # 每次扫描从池里随机测的 IP 数量
TTL = 7 * 86400    # 池有效期 7 天（过期回退全量重建）

_pools: dict | None = None
_pools_ts: float = 0


def _load():
    global _pools, _pools_ts
    try:
        d = json.loads(POOL_FILE.read_text())
        _pools = {k: d[k]["ips"] for k in d if isinstance(d[k], dict) and "ips" in d[k]}
        _pools_ts = d.get("ts", 0)
    except Exception:
        _pools, _pools_ts = {}, 0


def _save():
    global _pools
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        POOL_FILE.write_text(json.dumps({"ts": time.time(),
                                         **{k: {"ips": v} for k, v in (_pools or {}).items()}}))
    except Exception:
        pass


def get(code: str) -> list:
    """取某 DC 的 IP 池（可能为空）。"""
    global _pools
    if _pools is None:
        _load()
    if _pools_ts and time.time() - _pools_ts > TTL:
        return []
    return list((_pools or {}).get(code.upper(), []))


def size(code: str) -> int:
    return len(get(code))


def add(code: str, ips: list, max_size: int = POOL_SIZE):
    """把验证成功的 IP 并入池（去重、保持顺序、截断）。"""
    global _pools
    if _pools is None:
        _load()
    if _pools is None:
        _pools = {}
    pool = _pools.setdefault(code.upper(), [])
    for ip in ips:
        if ip and ip not in pool:
            pool.append(ip)
    if len(pool) > max_size:
        pool[:] = pool[-max_size:]
    _save()


def pool_report() -> dict:
    """池统计：{code: n}，供 /api/pools 展示。"""
    global _pools
    if _pools is None:
        _load()
    return {c: len(v) for c, v in (_pools or {}).items() if v}


def pools_detail() -> list:
    """池明细（供前端 IP 池面板）：[{code, cc, cc_zh, size, ips}]，按 size 降序。"""
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
    global _pools
    if _pools is None:
        _load()
    if _pools is None:
        _pools = {}
    n = len(_pools.get(code.upper(), []))
    _pools.pop(code.upper(), None)
    if n:
        _save()
    return n


def clear_all() -> int:
    """清空全部池，返回总 IP 数。"""
    global _pools
    if _pools is None:
        _load()
    n = sum(len(v) for v in (_pools or {}).values())
    _pools = {}
    _save()
    return n


def probe_and_add(ips: list, code_hint: str = "", use_v6: bool = False,
                  use_tls: bool = True, workers: int = 12, log=None) -> dict:
    """手动补充 IP 入池：先做 CF 官方 IP 段校验，再并发探测实际 colo。

    - 不在 CF 官方 CIDR 内的 IP 会被拒绝（不入库、计入 rejected）。
    - 探测成功且命中目标节点（code_hint 为空时按探测到的 colo 入库）→ 入池。
    - 探测成功但 colo 与 code_hint 不一致 → 计入 mismatch，不入库。
    - 探测失败（超时/无响应）→ 计入 failed，不入库。

    返回 {"added": n, "rejected": n, "mismatch": n, "failed": n,
          "by_colo": {code: [ips...]}, "details": [{ip, ok, colo, reason}]}
    """
    import concurrent.futures as cfu

    global _pools
    if _pools is None:
        _load()
    if _pools is None:
        _pools = {}

    # 1. CF 官方 IP 段校验（ipdata.fetch_cf_ips 7 天缓存）
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
        for n in nets:
            if n.version == a.version and a in n:
                return True
        return False

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

    # 2. 并发探测实际 colo
    by_colo: dict = {}
    failed = []

    def probe(ip):
        try:
            cc, colo, city = ipdata.probe_location(ip, use_tls, timeout=4)
            return ip, cc, colo, city, None
        except Exception as e:
            return ip, None, None, None, str(e)

    lock = threading.Lock()
    with cfu.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe, ip) for ip in pending]
        for f in cfu.as_completed(futs):
            ip, cc, colo, city, err = f.result()
            if log:
                log(f"探测 {ip} → {colo or '(失败)'}")
            if err:
                details.append({"ip": ip, "ok": False, "reason": f"探测异常：{err}"})
                failed.append(ip)
                continue
            if not colo:
                details.append({"ip": ip, "ok": False, "reason": "探测无响应/无 colo 头"})
                failed.append(ip)
                continue
            # code_hint 为空：按实际 colo 归池；非空：必须匹配
            target = (code_hint or "").upper()
            if target and colo.upper() != target:
                details.append({"ip": ip, "ok": False,
                                "reason": f"实际 colo={colo} 与目标 {target} 不符"})
                continue
            with lock:
                by_colo.setdefault(colo.upper(), []).append(ip)

    # 3. 入池
    added = 0
    for colo, ip_list in by_colo.items():
        before = len((_pools or {}).get(colo, []))
        add(colo, ip_list)
        delta = len((_pools or {}).get(colo, [])) - before
        added += delta
        if log:
            log(f"  → 入池 {colo}：+{delta}")

    # 4. 汇总
    added_ips = [ip for v in by_colo.values() for ip in v]
    return {
        "added": added,
        "rejected": len(ips) - len(pending),
        "mismatch": sum(1 for d in details if "不符" in d.get("reason", "")),
        "failed": len(failed),
        "by_colo": by_colo,
        "details": details,
    }


def preheat(codes: list, use_v6: bool = False, use_tls: bool = True, workers: int = 12, log=None) -> dict:
    """启动预热：用采样探测为指定 DC 建池（不依赖 geoip 目标匹配）。

    直接探测候选 IP 的 cf-meta-colo，命中目标 DC 的入库。
    返回 {code: 池大小}。log: 可选回调 (msg)。
    """
    global _pools
    if _pools is None:
        _load()
    need = {c.upper(): max(1, POOL_SIZE - len(get(c))) for c in codes if len(get(c)) < POOL_SIZE}
    if not need:
        return {c: len(get(c)) for c in codes}
    if log:
        log(f"IP 池预热：{len(need)} 个节点缺额 {sum(need.values())}")

    import concurrent.futures as cfu

    want = min(1500, max(200, sum(need.values()) * 4))
    raw, _mp, _tp, _fb = ipdata.sample_cf_ips(want, use_v6, None, None)
    pool = list(raw[:want])
    random.shuffle(pool)
    probe_n = min(200, len(pool))

    def probe(ip):
        cc, colo, city = ipdata.probe_location(ip, use_tls, timeout=4)
        return ip, cc, colo, city

    lock = threading.Lock()
    with cfu.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, ip): ip for ip in pool[:probe_n]}
        for f in cfu.as_completed(futs):
            ip, cc, colo, city = f.result()
            if not colo:
                continue
            with lock:
                if len(get(colo)) < POOL_SIZE:
                    add(colo, [ip])
    return {c: len(get(c)) for c in codes}


def preheat_nearby(cc_codes: list, use_v6: bool = False, use_tls: bool = True, log=None) -> dict:
    """启动预热：目标国家组下全部 DC 建池。"""
    from . import geoip
    cc2colos = geoip.colo_list_by_cc()
    codes = [cd["code"] for cc in cc_codes for cd in cc2colos.get(cc.upper(), [])]
    if not codes:
        return {}
    return preheat(codes, use_v6, use_tls, log=log)
