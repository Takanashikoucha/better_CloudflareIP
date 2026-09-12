# -*- coding: utf-8 -*-
"""扫描历史：自动保存最近 HISTORY_LIMIT 次，支持查看 / 复用参数 / 删除 / 清空。

持久化统一走 store（原子写 + 单锁）。
"""
import time
from datetime import datetime

from . import config, store


def load() -> list:
    return store.load_history()


def save(h: list):
    store.save_history(h)


def add(payload: dict, params: dict):
    """追加一条历史（payload = 扫描结果，params = 扫描参数）。"""
    h = load()
    entry = {
        "id": int(time.time() * 1000),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **payload,
        "params": params,
    }
    h.insert(0, entry)
    save(h[:config.HISTORY_LIMIT])


def delete(entry_id: int) -> bool:
    h = load()
    kept = [x for x in h if x.get("id") != entry_id]
    if len(kept) == len(h):
        return False
    save(kept)
    return True


def clear():
    save([])
