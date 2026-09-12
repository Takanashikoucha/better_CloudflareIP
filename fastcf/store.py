# -*- coding: utf-8 -*-
"""统一持久化层：JSON 文件的原子读写 + 进程级单锁。

所有持久化数据（源缓存 / IP 池 / 历史）都经过本模块，
避免各模块各自裸读写文件、各自加锁导致的状态散落。

文件布局（DATA_DIR 下）：
  cf_ips.json / ext_ips.json   双源缓存（7 天 TTL，过期自动刷新，失败沿用旧缓存）
  colo_data.json              colo 参考表在线刷新结果（3 天 TTL，失败沿用内置快照）
  ip_pools.json               DC 级 IP 池 {DC: {ips, ts}}
  history.json                最近 50 次扫描记录
"""
import json
import threading
import time

from . import config
from .net import atomic_write

_lock = threading.Lock()

CF_IPS_CACHE = config.DATA_DIR / "cf_ips.json"
EXT_IPS_CACHE = config.DATA_DIR / "ext_ips.json"
POOL_FILE = config.DATA_DIR / "ip_pools.json"
HISTORY_FILE = config.DATA_DIR / "history.json"


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_json(path, default):
    with _lock:
        return _read_json(path, default)


def write_json(path, obj):
    with _lock:
        try:
            atomic_write(path, json.dumps(obj, ensure_ascii=False))
        except OSError as e:
            # 只读文件系统（如沙盒环境）：静默失败，不中断扫描
            print(f"[store] 写入失败 {path.name}（{e}），继续使用内存数据", flush=True)


def read_text(path):
    with _lock:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None


def write_text(path, text: str):
    with _lock:
        atomic_write(path, text)


# ── 源缓存（带 TTL 语义的读写）──

def cached_source(path, force: bool = False) -> dict | None:
    """TTL 内直接复用缓存；否则返回 None（调用方负责刷新）。"""
    if not force:
        d = read_json(path, None)
        if d and d.get("v4") and time.time() - d.get("ts", 0) < config.CACHE_TTL:
            return d
    return None


def stale_source(path) -> dict | None:
    """下载失败时沿用旧缓存（哪怕过期）。"""
    d = read_json(path, None)
    if d and d.get("v4"):
        return d
    return None


def save_source(path, obj: dict):
    write_json(path, obj)


# ── IP 池 ──

def load_pools() -> tuple:
    """返回 (pools, pool_ts)：{DC: [ips]} / {DC: ts}。"""
    d = read_json(POOL_FILE, {})
    pools = {k: v["ips"] for k, v in d.get("pools", {}).items() if v.get("ips")}
    pool_ts = {k: v.get("ts", 0) for k, v in d.get("pools", {}).items()}
    return pools, pool_ts


def save_pools(pools: dict, pool_ts: dict):
    write_json(POOL_FILE, {
        "ts": time.time(),
        "pools": {k: {"ips": v, "ts": pool_ts.get(k, 0)} for k, v in pools.items()},
    })


# ── 历史 ──

def load_history() -> list:
    return read_json(HISTORY_FILE, [])


def save_history(h: list):
    write_json(HISTORY_FILE, h)
