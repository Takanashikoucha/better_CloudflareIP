# -*- coding: utf-8 -*-
"""后台常驻 IP 池填充线程。

- 启动即开始：为就近 DC（默认 US/JP/HK 组）持续采样建池，直到池量达标
- 池整体过期后自动重新探测（pools.refill 内部处理），过期 IP 不直接删除
- 每次扫描成功后，若扫描命中的 DC 池量不足，顺带补池（scan_done 唤醒）
- 温和节流：4 并发 + 0.3s 间隔；池量达标后休眠等待唤醒
"""
import threading
import time

from . import geoip, pools

DEFAULT_TARGETS = ["US", "JP", "HK"]

# 扫描进行中时 filler 让路（避免与测速抢带宽）
_scan_busy = threading.Event()


def scan_started():
    _scan_busy.set()


def scan_finished():
    _scan_busy.clear()


class PoolFiller(threading.Thread):
    """常驻填充线程（daemon）。"""

    def __init__(self, target_ccs: list | None = None,
                 use_v6: bool = False, use_tls: bool = True,
                 log=None):
        super().__init__(daemon=True, name="pool-filler")
        self.target_ccs = [c.upper() for c in (target_ccs or DEFAULT_TARGETS)]
        self.use_v6 = use_v6
        self.use_tls = use_tls
        self.log = log or (lambda m: None)
        self._wake = threading.Event()
        self._stop = False
        self._target_codes = self._resolve_codes()

    def _resolve_codes(self) -> list:
        cc2colos = geoip.colo_list_by_cc()
        return [cd["code"] for cc in self.target_ccs
                for cd in cc2colos.get(cc, [])]

    def _need_fill(self) -> bool:
        if pools.expired():
            return True
        codes = self._target_codes
        return any(pools.size(c) < pools.POOL_SIZE for c in codes)

    def wake_for(self, codes: list):
        """扫描结束唤醒：检查指定 DC 是否缺额，缺额则立即补一轮。"""
        need = [c.upper() for c in codes if pools.size(c) < pools.POOL_SIZE]
        if need:
            self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _fill_round(self, stop_check):
        """一轮填充：先处理过期重探/目标 DC 缺额，再补池。"""
        codes = [c for c in self._target_codes if pools.size(c) < pools.POOL_SIZE]
        pools.refill(codes or None, self.use_v6, self.use_tls,
                     max_probes=100, stop=stop_check, log=self.log)

    def run(self):
        self.log(f"后台填充启动：目标 {geoip.countries_zh(self.target_ccs)}"
                 f"（{len(self._target_codes)} 个节点，每节点 {pools.POOL_SIZE} IP）")
        while not self._stop:
            # 扫描进行中：让路休眠
            while _scan_busy.is_set() and not self._stop:
                self._wake.wait(timeout=2)
                if self._stop:
                    break
            if self._stop:
                break
            if not self._need_fill():
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue

            def stop_check():
                return self._stop or _scan_busy.is_set()

            try:
                self._fill_round(stop_check)
            except Exception as e:
                self.log(f"后台填充异常：{e}")
            self._wake.wait(timeout=10)
            self._wake.clear()
        self.log("后台填充线程退出")


_filler: PoolFiller | None = None


def start(target_ccs: list | None = None, log=None) -> PoolFiller:
    """启动全局填充线程（幂等）。"""
    global _filler
    if _filler is None or not _filler.is_alive():
        _filler = PoolFiller(target_ccs, log=log)
        _filler.start()
    return _filler


def stop():
    global _filler
    if _filler:
        _filler.stop()
        _filler = None
