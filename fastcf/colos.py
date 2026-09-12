# -*- coding: utf-8 -*-
"""节点（colo）参考数据：code → 国家 / 中文名 映射。

数据源：内置静态快照（data_colos.py，Netrvin/cloudflare-colo-list 快照），
启动时可在线刷新（3 天 TTL，失败沿用快照）。
中文映射（国家/城市）见 zhnames.py。
"""
import json
import threading
import time

from . import config
from .data_colos import COLO_ZH as _STATIC
from .net import download
from .zhnames import COUNTRY_ZH, _CITY_ZH


def _name_zh(v: dict) -> str:
    """参考库条目 → 中文名 '国·城市'。"""
    cc = (v.get("cca2") or "").upper()
    country = COUNTRY_ZH.get(cc, cc)
    city = (v.get("city") or "").strip()
    city_zh = _CITY_ZH.get(city)
    if not city_zh:
        return f"{country}·{city}" if city else country
    core = country.rstrip("国")
    if city_zh == country or city_zh == core or city_zh in country or country in city_zh:
        return country
    return f"{country}·{city_zh}"


class Colos:
    """colo 参考表（线程安全，进程级单例）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {c: tuple(v) for c, v in _STATIC.items()}
        self._ts = 0.0

    def refresh(self, force: bool = False) -> bool:
        """在线刷新（3 天 TTL）；失败沿用快照，不视为错误。"""
        if not force:
            if self._ts and time.time() - self._ts < config.COLO_TTL:
                return True
            if config.COLO_DATA_PATH.exists():
                try:
                    if time.time() - config.COLO_DATA_PATH.stat().st_mtime < config.COLO_TTL:
                        return True
                except OSError:
                    pass
        for url in config.COLO_URLS:
            try:
                raw = json.loads(download(url, min_size=1000))
                new = {}
                for code, v in raw.items():
                    cca2 = (v.get("cca2") or "").upper()
                    name = _name_zh(v)
                    if code and name:
                        new[code.upper()] = (cca2, name)
                if new:
                    with self._lock:
                        self._data.update(new)
                        self._ts = time.time()
                    try:
                        config.COLO_DATA_PATH.write_text(
                            json.dumps({c: {"cca2": v[0], "name": v[1]} for c, v in new.items()},
                                      ensure_ascii=False))
                    except OSError:
                        pass
                    return True
            except Exception:
                continue
        return bool(self._data)

    def zh(self, code: str) -> str:
        """code → 中文节点名（如 '中国·香港'）。未收录返回原码。"""
        if not code:
            return ""
        with self._lock:
            v = self._data.get(code.strip().upper())
        return v[1] if v else code.strip().upper()

    def country(self, code: str):
        """code → ISO alpha2 国家码。未收录返回 None。"""
        if not code:
            return None
        with self._lock:
            v = self._data.get(code.strip().upper())
        return v[0].upper() if v and v[0] else None

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def groups(self) -> list:
        """按国家分组：[{cc, cc_zh, items: [{code, name}]}]，中国系置顶，组内按 code 排序。"""
        out: dict = {}
        with self._lock:
            for code, (cca2, name) in self._data.items():
                if cca2:
                    out.setdefault(cca2, []).append({"code": code, "name": name})
        groups = []
        for cc, items in out.items():
            items.sort(key=lambda x: x["code"])
            groups.append({"cc": cc, "cc_zh": COUNTRY_ZH.get(cc, cc), "items": items})
        _CN = ("CN", "HK", "MO", "TW")
        groups.sort(key=lambda g: (0 if g["cc"] in _CN else 1, g["cc_zh"], g["cc"]))
        return groups


def country_zh(code: str) -> str:
    if not code:
        return "未知"
    return COUNTRY_ZH.get(code.strip().upper(), code.strip().upper())


colos = Colos()  # 进程级单例
