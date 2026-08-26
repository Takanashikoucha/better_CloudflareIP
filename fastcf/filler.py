# -*- coding: utf-8 -*-
"""后台常驻 IP 池填充线程。

- 启动即开始**全量子网遍历**（/24 v4 或 /48 v6）：每子网探 1 个代表 IP 读
  实际 colo，命中则批量入池。首次遍历完成前禁止开始测速（见 scanner）。
- 首次遍历完成后切换回**缺额维护模式**：
  · 池整体过期 → 重新探测（pools.refill 内部处理）
  · 指定 DC 缺额 → 采样补池
  · 每次扫描成功后，若命中 DC 池量不足，顺带补池（scan_done 唤醒）
- 温和节流：并发 + 批间间隔；无缺额时休眠等待唤醒
"""
import threading
import time

from . import geoip, pools

# 首次遍历默认只扫 IPv4（IPv6 空间 2^128，物理上不可全量遍历）
DEFAULT_USE_V6 = False
SWEEP_WORKERS = 8      # 全量遍历并发数
SWEEP_PAUSE = 0.2      # 全量遍历批间停顿（秒，每 400 个批一次）
SWEEP_IPS = 10         # 命中子网批量入池的 IP 数

# 扫描进行中时 filler 让路（避免与测速抢带宽）
_scan_busy = threading.Event()


def scan_started():
    _scan_busy.set()


def scan_finished():
    _scan_busy.clear()


class PoolFiller(threading.Thread):
    """常驻填充线程（daemon）。"""

    def __init__(self, use_v6: bool = DEFAULT_USE_V6, use_tls: bool = True,
                 log=None):
        super().__init__(daemon=True, name="pool-filler")
        self.use_v6 = use_v6
        self.use_tls = use_tls
        self.log = log or (lambda m: None)
        self._wake = threading.Event()
        self._stop = False
        self._state_lock = threading.Lock()
        # 全量遍历状态（前端展示 + 测速闸门）
        self.sweep_done = False       # 首次遍历是否已完成
        self.sweep_started_ts = None
        self.sweep_finished_ts = None
        self.sweep_detail = ""        # 进度描述（如 1234/5956，命中 567）

    # ── 状态 ──

    def status(self) -> dict:
        """首次遍历状态（供 /api/status、/api/data-status）。"""
        with self._state_lock:
            return {
                "done": self.sweep_done,
                "detail": self.sweep_detail,
                "started": self.sweep_started_ts,
                "finished": self.sweep_finished_ts,
            }

    def _set_sweep(self, detail: str, done: bool = None):
        with self._state_lock:
            self.sweep_detail = detail
            if done is not None:
                self.sweep_done = done
                if done and self.sweep_finished_ts is None:
                    self.sweep_finished_ts = time.time()
            elif self.sweep_started_ts is None:
                self.sweep_started_ts = time.time()

    # ── 缺额维护（首次遍历完成后） ──

    def _need_fill(self) -> bool:
        if pools.expired():
            return True
        return any(pools.size(c) < pools.POOL_SIZE for c in pools.all_codes())

    def wake_for(self, codes: list):
        """扫描结束唤醒：检查指定 DC 是否缺额，缺额则立即补一轮。"""
        need = [c.upper() for c in codes if pools.size(c) < pools.POOL_SIZE]
        if need:
            self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    # ── 阶段一：全量子网遍历 ──

    def _run_sweep(self, stop_check):
        def sweep_log(msg):
            self.log(f"[遍历] {msg}")
            self._set_sweep(msg)

        pools.full_sweep(use_v6=self.use_v6, use_tls=self.use_tls,
                         ips_per_subnet=SWEEP_IPS, workers=SWEEP_WORKERS,
                         pause=SWEEP_PAUSE, stop=stop_check, log=sweep_log)

    # ── 阶段二：缺额维护 ──

    def _fill_round(self, stop_check):
        codes = [c for c in pools.all_codes() if pools.size(c) < pools.POOL_SIZE]
        pools.refill(codes or None, self.use_v6, self.use_tls,
                     max_probes=100, stop=stop_check, log=self.log)

    def run(self):
        self.log(f"后台填充启动：先做全量子网遍历（{'/48' if self.use_v6 else '/24'}）"
                 f"，完成前禁止测速；完成后转缺额维护模式")

        def stop_check():
            return self._stop

        # ── 阶段一：首次全量遍历 ──
        try:
            self._run_sweep(stop_check)
        except Exception as e:
            self.log(f"全量遍历异常：{e}")
        # 无论成功还是被中断，都放行测速（避免服务永久锁死）
        self._set_sweep("全量子网遍历完成，测速已解锁", done=True)
        self.log("✔ 首次全量子网遍历完成，测速已解锁")

        if self._stop:
            self.log("后台填充线程退出")
            return

        # ── 阶段二：缺额维护 ──
        self.log("进入缺额维护模式（池过期自动重探 / 缺额自动补池）")
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
            try:
                self._fill_round(stop_check)
            except Exception as e:
                self.log(f"后台填充异常：{e}")
            self._wake.wait(timeout=10)
            self._wake.clear()
        self.log("后台填充线程退出")


_filler: PoolFiller | None = None


def start(use_v6: bool = DEFAULT_USE_V6, log=None) -> PoolFiller:
    """启动全局填充线程（幂等）。默认先做全量子网遍历，完成前禁止测速。"""
    global _filler
    if _filler is None or not _filler.is_alive():
        _filler = PoolFiller(use_v6=use_v6, log=log)
        _filler.start()
    return _filler


def stop():
    global _filler
    if _filler:
        _filler.stop()
        _filler = None
