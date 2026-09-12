# -*- coding: utf-8 -*-
"""测速引擎：一次扫描的编排器（状态机 + SSE 事件流）。

固定口径：IPv4 · 443/TLS · 结果 5 个。

流程（指定 DC 与全局随机共用，只差候选来源）：
  A. 候选集：
     - 指定 DC：取该 DC 池（池为空 → 直接回退随机，不自动建池）
     - 全局随机：双源合并随机采样「随机 IP 数量」个
  B. ping 预筛：ICMP ping 并发 200（1 包探测 + 4 包精确测量），
     丢包 ≥75% 淘汰并从所属 DC 池剔除；时延 > 2× 最佳时延 淘汰（零丢包豁免）
  C. 下载测速：按延迟升序串行，443/TLS 下载
     - 随机 IP 测速前先探测 cf-meta-colo 确认实际 DC 并入池
     - 队列 = ping 预筛通过的全部候选，凑够 5 个达标 → 停止
     - 未达标（0Mbps/限流等）→ 继续测队列中下一个候选
  D. 回退：指定 DC 模式候选不足（池空 / 达标数 < 5）→ 随机模式再跑 B+C 补齐
  E. 汇总：按 延迟 → 丢包 → 速度 排序，输出 5 个；
     测速成功（>0Mbps）的 IP 回写其实际 DC 池

状态机：idle → running → done / error / cancelled。
所有状态变更通过 _emit 推送给 SSE 订阅者（带节流）。

依赖注入：Scanner 通过 EngineContext 接收 sources/pool/probe 等可替换依赖，
单测可离线跑完整流程（见 tests/test_units.py）。
"""
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import colos, config, pool
from .ping import ping, ping_probe
from .speedtest import probe_location, speed_test


class EngineContext:
    """扫描引擎依赖容器（默认指向真实实现，单测可替换）。"""

    def __init__(self):
        from . import sources
        self.sources = sources
        self.pool = pool
        self.colos = colos.colos
        self.probe_location = probe_location
        self.speed_test = speed_test
        self.ping = ping
        self.ping_probe = ping_probe


def default_context() -> EngineContext:
    return EngineContext()


class Scanner:
    """一次扫描的编排器。"""

    def __init__(self, params: dict, ctx: EngineContext | None = None):
        self.p = params
        self.ctx = ctx or default_context()
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.subscribers: list = []
        self.last_state = None
        self.result_payload = None
        self.start_ts = None
        self._last_emit = 0.0   # set_progress 节流（monotonic）
        self._last_pct = -100
        self._log_total = 0     # 已推送日志条数（增量推送游标）
        self.elapsed = 0
        self.done = threading.Event()

    # ── 状态推送 ──

    def _emit(self, state: dict):
        with self._lock:
            self.last_state = state
            dead = []
            for q in self.subscribers:
                try:
                    if q.full():
                        dead.append(q)
                    else:
                        q.put_nowait(state)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in self.subscribers:
                    self.subscribers.remove(q)

    def log(self, msg, level="info"):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)
        with self._lock:
            logs = list(self.last_state.get("logs", [])) if self.last_state else []
            logs.append({"ts": ts, "msg": msg, "level": level})
            if len(logs) > config.LOG_LIMIT:
                # 环形裁剪：同步推进增量游标，避免 delta 越界
                cut = len(logs) - config.LOG_LIMIT
                logs = logs[cut:]
                self._log_total = max(0, self._log_total - cut)
            self.last_state = {**self.last_state, "logs": logs} if self.last_state else {"logs": logs}
        self._emit_state(self.last_state)

    def _emit_state(self, base: dict):
        """统一出口：附加增量日志（logDelta/logTotal）后推送。

        增量语义：logDelta = 自上次推送以来新增的日志；
        迟到订阅者首帧拿全量 last_state（logs 完整 + logDelta 为空），
        前端据此重置本地日志数组。
        """
        with self._lock:
            logs = self.last_state.get("logs", []) if self.last_state else []
            delta = logs[self._log_total:]
            self._log_total = len(logs)
        base = {**base, "logs": logs, "logDelta": delta, "logTotal": len(logs)}
        self._emit(base)

    def set_progress(self, stage, pct, detail=""):
        # 节流：至少 200ms 间隔，或 pct 前进 ≥2 才推送，避免 ping 2000 IP 时 SSE 刷屏
        pct = int(pct)
        now = time.monotonic()
        with self._lock:
            if now - self._last_emit < 0.2 and pct - self._last_pct < 2:
                return
            self._last_emit = now
            self._last_pct = pct
        self._emit_state({
            "running": True,
            "stage": stage,
            "pct": pct,
            "detail": detail,
            "elapsed": int(time.time() - self.start_ts) if self.start_ts else 0,
        })

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self._lock:
            self.subscribers.append(q)
            if self.last_state:
                try:
                    q.put_nowait(self.last_state)
                except Exception:
                    pass
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def cancel(self):
        self.cancel_event.set()
        self.log("用户已取消扫描", "warn")

    def _cancelled(self):
        return self.cancel_event.is_set()

    # ── 主流程 ──

    def run(self):
        p = self.p
        self.start_ts = time.time()
        speed_secs = max(3, min(60, float(p.get("speedSecs", 8))))
        speed_mb = max(5, min(1000, int(p.get("speedMB", 5))))
        min_speed = max(0.0, min(10000.0, float(p.get("minSpeed", 0) or 0)))
        random_count = max(10, min(2000, int(p.get("randomCount", 150))))
        mode = (p.get("mode") or "").strip().upper()
        colo = (p.get("colo") or "").strip().upper()
        if mode == "DC" and not re.fullmatch(r"[A-Z]{3}", colo):
            self._finish_error("指定 DC 模式需要有效的节点代码（如 HKG）")
            return

        self.set_progress("prepare", 5, "准备中")
        self.log(f"开始扫描：IPv4 · 443/TLS · 返回 Top{config.RESULT_COUNT}"
                 f" | 测速={speed_secs:.0f}s/{speed_mb}MB"
                 f"{' | 速度下限 ' + format(min_speed, 'g') + 'Mbps' if min_speed > 0 else ''}"
                 f" | 模式={'指定 DC ' + colo if mode == 'DC' else '全局随机'}"
                 + (f"（随机 {random_count} 个 IP）" if mode != "DC" else ""))

        speed_results = []
        fallback_used = False

        # ── 第一步：指定 DC 模式 ──
        if mode == "DC":
            self.log(f"指定节点：{self.ctx.colos.zh(colo)} ({colo})")
            pool_ips = self.ctx.pool.get(colo)
            if not pool_ips:
                self.log(f"{colo} IP 池为空 → 回退全局随机模式", "warn")
                fallback_used = True
            else:
                # 池过期：事件性重新探测（前台同步，非后台）
                if self.ctx.pool.expired(colo):
                    self.log(f"{colo} 池已过期，事件性重新探测 {len(pool_ips)} 个 IP…", "warn")
                    self.set_progress("revalidate", 10, f"重验 {colo} 池")
                    pool_ips = self._revalidate_pool(colo, pool_ips)
                ips = random.sample(pool_ips, min(len(pool_ips), config.TEST_SIZE))
                self.log(f"从 {colo} 池取 {len(ips)}/{len(pool_ips)} 个 IP")
                speed_results = self._speed_phase(ips, speed_secs, speed_mb, min_speed,
                                                 need=config.RESULT_COUNT, dc_hint=colo)
                if self._cancelled():
                    return
                if len(speed_results) < config.RESULT_COUNT:
                    self.log(f"指定 DC 达标 {len(speed_results)} 个 < {config.RESULT_COUNT}"
                             f" → 回退全局随机补到 {config.RESULT_COUNT} 个", "warn")
                    fallback_used = True

        # ── 第二步：全局随机模式（指定模式不足时的回退，或用户直接选随机）──
        # 注意：这里**不**提前 return——取消检查统一放到 speed_results 汇总处，
        # 保证取消时一定走 _finalize（否则 result_payload 为 None）。
        if mode != "DC" or fallback_used:
            existing = {r["ip"] for r in speed_results}
            need = config.RESULT_COUNT - len(existing)
            self.set_progress("geo", 12, f"全局随机采样 {random_count} 个 IP")
            self.log(f"全局随机模式：从全 CF IP 段随机采样 {random_count} 个"
                     f"{'（补齐 ' + str(need) + ' 个）' if need < config.RESULT_COUNT else ''}")
            ips = [ip for ip in self.ctx.sources.sample_cf_ips(random_count) if ip not in existing]
            random.shuffle(ips)
            more = self._speed_phase(ips, speed_secs, speed_mb, min_speed,
                                    need=need, random_pool=True)
            speed_results.extend(more)

        # 取消扫描：保留已测出的部分结果（无结果时也要 finalize，
        # 否则 result_payload 为 None，前端 /api/status 看不到取消状态）
        if self._cancelled():
            self.log(f"扫描已取消，保留 {len(speed_results)} 个已测结果", "warn")
            self._finalize(speed_results, mode, colo, fallback_used,
                          random_count, min_speed, cancelled=True)
            return
        if not speed_results:
            self._finish_error("没有可测速的 Cloudflare IP（网络异常或被拦截），请检查网络")
            return

        # ── 汇总 ──
        self._finalize(speed_results, mode, colo, fallback_used,
                       random_count, min_speed, cancelled=False)

    def _finalize(self, speed_results: list, mode: str, colo: str,
                  fallback_used: bool, random_count: int, min_speed: float,
                  cancelled: bool = False):
        """汇总结果：回写池、排序、取前 5、设置 result_payload、emit 最终态。"""
        # 成功（>0Mbps）的 IP 回写其实际 DC 池
        ok_by_dc = {}
        for r in speed_results:
            if r["mbps"] > 0 and r.get("dc"):
                ok_by_dc.setdefault(r["dc"].upper(), []).append(r["ip"])
        for dc, ip_list in ok_by_dc.items():
            self.ctx.pool.add(dc, ip_list)
        if ok_by_dc:
            self.log(f"回写池：{', '.join(f'{dc} +{len(v)}' for dc, v in ok_by_dc.items())}")

        speed_results.sort(key=lambda r: (
            r.get("ping") or 10**9,
            r.get("loss") if r.get("loss") is not None else 1.0,
            -r.get("mbps", 0),
        ))
        out = []
        for i, r in enumerate(speed_results[:config.RESULT_COUNT]):
            out.append({
                "rank": i + 1,
                "ip": r["ip"],
                "ping": r.get("ping", 0),
                "latency": r.get("ping", 0),
                "loss": r.get("loss", 0),
                "mbps": r.get("mbps", 0),
                "port": 443,
                "dc": r.get("dc", ""),
                "dc_zh": r.get("dc_zh", ""),
                "cfRay": r.get("cfRay", ""),
                "location": r.get("loc", ""),
                "tls": True,
            })

        self.elapsed = int(time.time() - self.start_ts)
        base_mode = ("DC" if mode == "DC" and not fallback_used else
                     ("DC+随机" if mode == "DC" and fallback_used else "RANDOM"))
        if cancelled:
            base_mode += "(取消)"
        self.result_payload = {
            "count": len(out),
            "elapsed": self.elapsed,
            "ipVer": "v4",
            "tls": True,
            "mode": base_mode,
            "cancelled": cancelled,
            "colo": colo or None,
            "randomCount": random_count,
            "minSpeed": min_speed,
            "results": out,
        }
        suffix = "（已取消）" if cancelled else ""
        self.log(f"扫描{'已取消' if cancelled else '完成'}，用时 {self.elapsed} 秒，"
                 f"返回 {len(out)} 个结果{suffix}（按 延迟/丢包/速度 排序）",
                 "warn" if cancelled else "ok")
        self._emit_state({"running": False, "stage": "done", "pct": 100, "detail": "",
                          "elapsed": self.elapsed})
        self.done.set()

    def _finish_error(self, msg):
        self.log(msg, "error")
        self.result_payload = {"error": msg}
        self._emit_state({"running": False, "stage": "error", "pct": 100, "detail": msg,
                          "elapsed": int(time.time() - (self.start_ts or time.time()))})
        self.done.set()

    # ── 事件性池重验（池过期且被指定 DC 扫描用到时，前台同步执行）──

    def _revalidate_pool(self, colo: str, ips: list) -> list:
        """并发 ping 池内 IP：成功 → 刷新时间戳；丢包 ≥75% → 剔除。"""
        alive, bad = [], []
        done = 0
        with ThreadPoolExecutor(max_workers=config.PING_WORKERS) as ex:
            futs = {ex.submit(self.ctx.ping, ip): ip for ip in ips}
            for fut in as_completed(futs):
                if self._cancelled():
                    ex.shutdown(wait=False)
                    return []
                ip = futs[fut]
                try:
                    _avg, loss = fut.result()
                except Exception:
                    loss = 1.0
                done += 1
                self.set_progress("revalidate", 10 + int(6 * done / len(ips)),
                                 f"重验 {done}/{len(ips)}")
                if loss >= config.LOSS_CUTOFF:
                    bad.append(ip)
                else:
                    alive.append(ip)
        if bad:
            self.ctx.pool.remove(colo, bad)
            self.log(f"重验：{colo} 剔除 {len(bad)} 个失效 IP，保留 {len(alive)} 个")
        else:
            self.log(f"重验：{colo} 全部 {len(alive)} 个 IP 有效")
        self.ctx.pool.touch(colo)
        return alive

    # ── B+C：ping 预筛 + 下载测速（一个阶段）──

    def _speed_phase(self, ips: list, speed_secs: float, speed_mb: int,
                     min_speed: float, need: int, dc_hint: str = "",
                     random_pool: bool = False) -> list:
        """对候选 IP 跑 ping 预筛 + 串行下载测速，返回达标结果（最多 need 个）。

        下载队列 = ping 预筛通过的全部候选（延迟升序）：按序逐个测速，
        直到凑够 need 个达标结果才停止；队列耗尽仍未凑够（限流/异常）
        则返回已达标部分（后续由上层回退随机模式补齐）。
        random_pool=True：随机 IP，下载测速前先探测 cf-meta-colo 入池。
        """
        if self._cancelled():
            return []
        pn = len(ips)
        if pn == 0:
            return []

        # ── B. ping 预筛（两阶段：1 包探测 + 4 包精确测量，并发 200）──
        # 阶段 1：1 包探测（快速淘汰不可达）
        self.set_progress("rtt", 20, f"ICMP 探测 {pn} 个 IP（1 包，并发 {config.PING_WORKERS}）")
        alive_ips = []
        done_count = 0
        with ThreadPoolExecutor(max_workers=config.PING_WORKERS) as ex:
            futs = {ex.submit(self.ctx.ping_probe, ip): ip for ip in ips}
            for fut in as_completed(futs):
                if self._cancelled():
                    ex.shutdown(wait=False)
                    return []
                ip = futs[fut]
                try:
                    reachable, _ = fut.result()
                except Exception:
                    reachable = False
                done_count += 1
                self.set_progress("rtt", 20 + int(10 * done_count / pn),
                                 f"探测 {done_count}/{pn}")
                if reachable:
                    alive_ips.append(ip)
        if not alive_ips:
            self.log("所有 IP 探测不可达（网络异常或被拦截）", "error")
            return []
        self.log(f"ICMP 探测完成：{len(alive_ips)}/{pn} 个存活 → 精确测量 {len(alive_ips)} 个（4 包）")

        # 阶段 2：4 包精确测量（仅对存活者）
        pn2 = len(alive_ips)
        self.set_progress("rtt", 30, f"ICMP 精确测量 {pn2} 个 IP（4 包，并发 {config.PING_WORKERS}）")
        ping_results = []
        done_count = 0
        with ThreadPoolExecutor(max_workers=config.PING_WORKERS) as ex:
            futs = {ex.submit(self.ctx.ping, ip): ip for ip in alive_ips}
            for fut in as_completed(futs):
                if self._cancelled():
                    ex.shutdown(wait=False)
                    return []
                ip = futs[fut]
                try:
                    avg_ms, loss = fut.result()
                except Exception:
                    avg_ms, loss = 0, 1.0
                done_count += 1
                self.set_progress("rtt", 30 + int(10 * done_count / pn2),
                                 f"测量 {done_count}/{pn2}")
                if avg_ms > 0:
                    ping_results.append({"ip": ip, "ping": avg_ms, "loss": loss})
        if not ping_results:
            self.log("所有存活 IP 精确测量失败（网络异常或被拦截）", "error")
            return []

        # 丢包 ≥75%：淘汰 + 从所属 DC 池剔除（反向索引 O(1) 定位，按 DC 聚合一次 remove）
        bad_ips = [r["ip"] for r in ping_results if r["loss"] >= config.LOSS_CUTOFF]
        if bad_ips:
            ip_index = self.ctx.pool.build_ip_index()
            bad_by_dc: dict = {}
            for ip in bad_ips:
                dc = ip_index.get(ip)
                if dc:
                    bad_by_dc.setdefault(dc, []).append(ip)
            for dc, ip_list in bad_by_dc.items():
                self.ctx.pool.remove(dc, ip_list)
            if bad_by_dc:
                self.log(f"ping 丢包 ≥{config.LOSS_CUTOFF:.0%}：从池中剔除 "
                         f"{sum(len(v) for v in bad_by_dc.values())} 个失效 IP")
        ping_results = [r for r in ping_results if r["loss"] < config.LOSS_CUTOFF]

        # 时延 > 2× 最佳时延 淘汰（零丢包者豁免）
        if ping_results:
            best_ping = min(r["ping"] for r in ping_results)
            filtered = [r for r in ping_results
                       if r["ping"] <= best_ping * config.PING_LAT_FACTOR or r["loss"] == 0]
            dropped = len(ping_results) - len(filtered)
            if dropped:
                self.log(f"时延过滤：淘汰 {dropped} 个（时延 > {int(best_ping * config.PING_LAT_FACTOR)}ms）")
            ping_results = filtered

        ping_results.sort(key=lambda r: (r["ping"], r["loss"]))
        queue_ = list(ping_results)  # 下载队列 = 全部预筛通过候选，按延迟升序
        self.log(f"ping 预筛完成：{len(ping_results)}/{pn} 个可达"
                 f"（探测存活 {len(alive_ips)}，最低丢包 {min(r['loss'] for r in ping_results):.0%}），"
                 f"全部 {len(queue_)} 个进入下载测速（凑够 {need} 个达标即停）")
        for r in queue_[:15]:
            self.log(f"  候选 {r['ip']}  ping {r['ping']}ms · 丢包 {r['loss']:.0%}")
        if len(queue_) > 15:
            self.log(f"  …（其余 {len(queue_) - 15} 个候选略）")

        # ── C. 下载测速（443/TLS，并发 SPEED_WORKERS，按延迟升序提交）──
        # 提交顺序 = 延迟升序（最优 IP 优先拿到结果）；
        # 凑够 need 个达标 → 取消未开始的 future（已完成的保留）；
        # 快速失败：前 3 个 IP 都 0Mbps → 提前停止（网络异常）；
        # 取消扫描 → 保留已完成结果。
        self.set_progress("speed", 45, "下载测速")
        sl_note = (f"，速度下限 {min_speed:g}Mbps" if min_speed > 0 else "") + f"，凑够 {need} 个即停"
        self.log(f"开始下载测速（队列 {len(queue_)} 个，并发 {config.SPEED_WORKERS}{sl_note}）")
        results = []
        res_lock = threading.Lock()
        pending = {}  # future -> queue item
        fail_count = 0  # 连续失败计数（快速失败用）
        FAIL_LIMIT = 3  # 前 3 个都失败 → 提前停止

        def _measure(r):
            nonlocal fail_count
            ip = r["ip"]
            # 随机 IP：测速前探测实际服务节点，确认 DC 并入池（同一 worker 内串行）
            if random_pool:
                _cc, colo_hit, _city = self.ctx.probe_location(ip)
                if colo_hit:
                    self.ctx.pool.add(colo_hit.upper(), [ip], save=True)
                    self.log(f"  {ip} 实际节点 {self.ctx.colos.zh(colo_hit)} ({colo_hit})，已入池")
                else:
                    self.log(f"  {ip} 探测未读到实际节点，继续测速", "warn")
            res = self.ctx.speed_test(ip, speed_mb * 1024 * 1024, speed_secs,
                                      is_cancelled=self._cancelled)
            res["dc_zh"] = self.ctx.colos.zh(res.get("dc", ""))
            res["loc"] = res.get("dc_zh") or res.get("location") or ""
            res["ping"] = r["ping"]
            res["loss"] = r.get("loss", 0)
            res["port"] = 443
            ok = res["mbps"] > 0 if min_speed == 0 else res["mbps"] >= min_speed
            mark = " ✔达标" if ok else " ✘未达标"
            self.log(f"  {res['ip']}  ping {r['ping']}ms · 丢包 {r.get('loss', 0):.0%} · "
                     f"{res['mbps']} Mbps{mark} · {res['loc']}")
            if not ok:
                fail_count += 1
            return res, ok

        with ThreadPoolExecutor(max_workers=config.SPEED_WORKERS) as ex:
            for i, r in enumerate(queue_):
                if self._cancelled():
                    break
                if len(results) >= need:
                    break
                if fail_count >= FAIL_LIMIT:
                    self.log(f"快速失败：前 {FAIL_LIMIT} 个 IP 都 0Mbps，提前停止（网络异常或被拦截）", "error")
                    break
                self.set_progress("speed", 45 + int(45 * i / max(1, len(queue_))),
                                 f"测速 {i + 1}/{len(queue_)}：{r['ip']}")
                fut = ex.submit(_measure, r)
                pending[fut] = r
            # 按完成顺序收集；凑够 need 个达标即取消未开始的 future
            for fut in as_completed(pending):
                if self._cancelled():
                    break
                try:
                    res, ok = fut.result()
                except Exception:
                    res, ok = None, False
                if res is not None and ok:
                    with res_lock:
                        results.append(res)
                if len(results) >= need:
                    for f in pending:
                        f.cancel()
                    break
        if self._cancelled():
            self.log(f"已取消：保留 {len(results)} 个已完成测速结果", "warn")
        return results
