# -*- coding: utf-8 -*-
"""应用状态：单一事实来源（Single Source of Truth）。

AppState 聚合所有跨模块共享的状态：
- 扫描状态机（idle / running / done / error / cancelled）
- 最近一次结果 + 参数
- 历史 / 池 / 数据源缓存 的访问入口

所有 API 端点与 SSE 流都从 AppState 读取，避免状态散落。
"""
import sys
import threading
import time

from . import __version__, colos, history, pool, sources
from .scanner import Scanner


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.scanner: Scanner | None = None
        self.last_result: dict | None = None
        self.last_params: dict | None = None

    # ── 扫描状态机 ──

    @property
    def running(self) -> bool:
        # 以 done 事件为准（消除对 last_state 首次 emit 时序的依赖）：
        # 扫描器存在且尚未结束 = 运行中
        with self.lock:
            return bool(self.scanner and not self.scanner.done.is_set())

    def start(self, params: dict) -> tuple:
        # 注意：线程必须在释放 self.lock 之后启动——
        # Scanner.__init__ 内部会取自己的锁（log/_emit），
        # 若持锁启动会形成 self.lock → scanner._lock 的锁序，
        # 与 cancel()（先 self.lock 再 scanner.cancel → log → scanner._lock）死锁。
        if self.running:
            return False, "扫描正在进行中"
        with self.lock:
            self.scanner = Scanner(params)
            self.last_result = None
            sc = self.scanner
        t = threading.Thread(target=self._run, args=(sc, params), daemon=True)
        t.start()
        return True, ""

    def _run(self, sc: Scanner, params: dict):
        try:
            sc.run()
        except Exception as e:
            sc._finish_error(f"扫描异常：{e}")
        finally:
            # 兜底：无论 run() 以何种方式退出（含异常路径漏掉 done.set()），
            # 都保证 done 事件置位，否则 wait 方会永久挂起。
            sc.done.set()
        if sc.result_payload and "error" not in sc.result_payload:
            with self.lock:
                self.last_result = sc.result_payload
                self.last_params = params
            history.add(sc.result_payload, params)

    def cancel(self):
        with self.lock:
            if self.scanner:
                self.scanner.cancel()

    def status(self) -> dict:
        """当前扫描状态 + 最近一次结果（/api/status）。"""
        with self.lock:
            sc = self.scanner
            result = self.last_result
            params = self.last_params
        out = {}
        if sc is not None and sc.result_payload and "error" in sc.result_payload:
            out["error"] = sc.result_payload["error"]
        if result:
            out["result"] = result
            out["params"] = params
        out["running"] = self.running
        return out

    # ── 系统概要（/api/data-status）──

    def data_status(self) -> dict:
        rep = pool.pool_report()
        src = sources.sources_status()
        return {
            "version": __version__,
            "data_dir": str(sources.config.DATA_DIR),
            "cf_cidrs": src["official"]["n"],
            "cf_ts": src["official"]["ts"],
            "ext_ips": src["external"]["n"],
            "ext_ts": src["external"]["ts"],
            "pool_dc": len(rep),
            "pool_ips": sum(rep.values()),
            "pool_expired": pool.expired(),
            "colo_count": colos.colos.count(),
            "running": self.running,
            "python": ".".join(str(x) for x in sys.version_info[:3]),
        }
