# -*- coding: utf-8 -*-
"""DC 级 IP 池（仅 IPv4）。

每个 DC（colo）维护约 POOL_SIZE 个已验证可直连的 IP。
任何探测行为只要读到实际服务节点（cf-meta-colo），就把 IP 写回
**实际命中的那个 DC**（无目标过滤）。

入池途径（无后台线程，全部前台/事件性）：
  1. 手动探测并添加（probe_and_add：官方段校验 + cf-meta-colo 归池）
  2. 扫描中随机 IP 测速前探测入池
  3. 测速成功的 IP 回写其实际 DC

TTL 语义：池按"最后入池时间"过期（默认 7 天）。过期**不删除** IP、
不后台重探；当指定 DC 扫描用到该池时触发**事件性**重新探测
（scanner._revalidate_pool）：成功刷新时间戳、丢包严重剔除。
"""
import ipaddress
import json
import os
import threading
import time
from pathlib import Path

from . import geoip, ipdata

DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
POOL_FILE = DATA_DIR / "ip_pools.json"

POOL_SIZE = 50       # 每个 DC 池的 IP 上限
TEST_SIZE = 50       # 指定 DC 扫描时从池里最多取的 IP 数量
TTL = 7 * 86400      # 池有效期（过期触发事件性重探，不删除）

_pool: dict | None = None          # {DC: [ips...]}
_pool_ts: dict = {}                # {DC: 该 DC 池最后刷新时间}
_pools_ts: float = 0               # 整体最后刷新时间
_lock = threading.Lock()


def _load():
    global _pool, _pool_ts, _pools_ts
    try:
        d = json.loads(POOL_FILE.read_text())
        _pool = {k: v["ips"] for k, v in d.get("pools", {}).items() if v.get("ips")}
        _pool_ts = {k: v.get("ts", 0) for k, v in d.get("pools", {}).items()}
        _pools_ts = d.get("ts", 0)
    except Exception:
        _pool, _pool_ts, _pools_ts = {}, {}, 0


def _save():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        POOL_FILE.write_text(json.dumps(
            {"ts": time.time(),
             "pools": {k: {"ips": v, "ts": _pool_ts.get(k, 0)} for k, v in (_pool or {}).items()}}))
    except Exception:
        pass


def _ensure_loaded():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _load()


def get(code: str) -> list:
    """取某 DC 的 IP 池（可能为空）。"""
    _ensure_loaded()
    return list((_pool or {}).get(code.upper(), []))


def size(code: str) -> int:
    return len(get(code))


def add(code: str, ips: list, max_size: int = POOL_SIZE, save: bool = True):
    """把验证成功的 IP 并入指定 DC 的池（去重、保持顺序、截断保留最新）。"""
    _ensure_loaded()
    with _lock:
        pool = _pool.setdefault(code.upper(), [])
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
        pool = _pool.get(code.upper())
        if not pool:
            return
        before = len(pool)
        pool[:] = [ip for ip in pool if ip not in set(ips)]
        if len(pool) < before:
            _save()


def touch(code: str):
    """刷新某 DC 池的时间戳（事件性重验成功时调用）。"""
    _ensure_loaded()
    with _lock:
        if code.upper() in (_pool or {}):
            _pool_ts[code.upper()] = time.time()
            _pools_ts = time.time()
            _save()


def expired(code: str = "") -> bool:
    """池是否超过 TTL。code 非空 → 判断单 DC；为空 → 判断整体。
    从未保存过（时间戳 0）不视为过期。"""
    _ensure_loaded()
    if code:
        ts = _pool_ts.get(code.upper(), 0)
        return bool(ts) and time.time() - ts > TTL
    return bool(_pools_ts) and time.time() - _pools_ts > TTL


def all_codes() -> list:
    _ensure_loaded()
    return list((_pool or {}).keys())


def pool_report() -> dict:
    """池统计：{code: n}。"""
    _ensure_loaded()
    return {c: len(v) for c, v in (_pool or {}).items() if v}


def pools_detail() -> list:
    """池明细（前端面板）：[{code, cc, cc_zh, size, ips, expired}]，按 size 降序。"""
    rep = pool_report()
    out = []
    for code, n in rep.items():
        cc = geoip.colo_country(code) or ""
        out.append({
            "code": code,
            "cc": cc,
            "cc_zh": geoip.country_zh(cc) if cc else "",
            "size": n,
            "ips": list((_pool or {}).get(code, [])),
            "expired": expired(code),
        })
    out.sort(key=lambda x: (-x["size"], x["code"]))
    return out


def clear_pool(code: str) -> int:
    """清空指定 DC 的池，返回被删 IP 数。"""
    _ensure_loaded()
    with _lock:
        n = len(_pool.get(code.upper(), []))
        _pool.pop(code.upper(), None)
        _pool_ts.pop(code.upper(), None)
        if n:
            _save()
    return n


def clear_all() -> int:
    """清空全部池，返回总 IP 数。"""
    global _pool, _pool_ts, _pools_ts
    _ensure_loaded()
    with _lock:
        n = sum(len(v) for v in (_pool or {}).values())
        _pool = {}
        _pool_ts = {}
        _pools_ts = 0
        if n:
            _save()
    return n


def _probe(ip, use_tls=True, timeout=4):
    """探测单个 IP 的实际服务节点。返回 (ip, colo, err)。"""
    try:
        _cc, colo, _city = ipdata.probe_location(ip, use_tls, timeout=timeout)
        return ip, colo, None
    except Exception as e:
        return ip, None, str(e)


def probe_and_add(ips: list, code_hint: str = "", use_tls: bool = True,
                  workers: int = 12, log=None) -> dict:
    """手动补充 IP 入池（手动探测并添加功能）：

    1. CF 官方 IPv4 段校验（不在段内 → rejected）
    2. 并发探测 cf-meta-colo 实际服务节点
       - code_hint 为空：按实际 colo 归池
       - code_hint 非空：结果必须匹配，否则 mismatch
    """
    import concurrent.futures as cfu

    cf_cache = ipdata.fetch_cf_ips()
    nets = []
    for c in cf_cache.get("v4", []):
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except Exception:
            pass

    def in_cf(ip_str: str) -> bool:
        try:
            a = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return a.version == 4 and any(a in n for n in nets)

    details = []
    pending = []
    for raw in ips:
        ip = raw.strip()
        if not ip:
            continue
        if not in_cf(ip):
            details.append({"ip": ip, "ok": False, "reason": "不在 CF 官方 IPv4 段"})
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
