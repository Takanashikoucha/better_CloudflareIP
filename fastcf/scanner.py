# -*- coding: utf-8 -*-
"""测速引擎：采样 → ping 预筛选 → 逐节点下载测速 → 按 ping 排名。

所有网络 I/O 均为直连 socket（模块导入时已清除代理环境变量），
不经过任何系统代理。
"""
import concurrent.futures as _cf
import queue
import random
import socket
import ssl
import threading
import time

from . import geoip, ipdata, pools

# 每轮扫描最多保留的 RTT 候选数（默认值；可被 params["top_rtt"] 覆盖）
TOP_RTT = 10
# ping 预筛选：每个候选 N 次拨号（对齐 CFST 的 PingTimes=4）
PING_TIMES = 4
# ping 预筛选：平均延迟超过 2× 最小时延 的候选直接淘汰（对齐 CFST 的相对过滤）
PING_LAT_FACTOR = 2.0


class Scanner:
    """一次扫描的编排器。"""

    def __init__(self, params: dict):
        self.p = params
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.last_state = None
        self.result_payload = None
        self.start_ts = None
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
            if self.last_state is None:
                logs = []
            else:
                logs = list(self.last_state.get("logs", []))
            logs.append({"ts": ts, "msg": msg, "level": level})
            if len(logs) > 400:
                logs = logs[-400:]
            self.last_state = {**self.last_state, "logs": logs} if self.last_state else {"logs": logs}
        self._emit(self.last_state)

    def set_progress(self, stage, pct, detail=""):
        with self._lock:
            logs = self.last_state.get("logs", []) if self.last_state else []
        self._emit({
            "running": True,
            "stage": stage,
            "pct": int(pct),
            "detail": detail,
            "elapsed": int(time.time() - self.start_ts) if self.start_ts else 0,
            "logs": logs,
        })

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=200)
        with self._lock:
            self.subscribers.append(q)
            if self.last_state:
                try:
                    q.put_nowait(self.last_state)
                except queue.Full:
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
        use_v6 = p.get("ipVer") == "v6"
        use_tls = bool(p.get("tls", True))
        sample_n = max(20, min(3000, int(p.get("sample", 150))))
        speed_secs = max(3, min(60, float(p.get("speedSecs", 8))))
        speed_mb = max(10, min(1000, int(p.get("speedMB", 50))))
        count = max(1, min(10, int(p.get("count", 5))))
        countries = [c for c in p.get("countries", geoip.DEFAULT_NEARBY)]
        colo = (p.get("colo") or "").strip().upper()  # ""=自动就近, "RANDOM"=全局随机, 其他=指定 DC
        top_rtt = max(3, min(30, int(p.get("top_rtt", TOP_RTT))))
        # 下载速度下限（Mbps）：>0 时下载测速"凑够 count 个达标 IP 即停"；0 = 测满 top_rtt 个
        min_speed = max(0.0, min(10000.0, float(p.get("minSpeed", 0) or 0)))

        self.set_progress("prepare", 5, "准备中")
        ver = "v6" if use_v6 else "v4"
        sl = f" | 速度下限 {min_speed:g}Mbps" if min_speed > 0 else ""
        self.log(f"开始扫描：{'IPv6' if use_v6 else 'IPv4'} | TLS={'开' if use_tls else '关'} | "
                  f"采样={sample_n} | 测速={speed_secs:.0f}s/{speed_mb}MB{sl} | 返回 Top{count}")

        # 1. 构建候选 IP 集（就近过滤常开）
        #    优先级：a) 指定 colo（DC 单点）→ 只用该 DC 的池
        #           b) 否则按国家组，合并命中国家的各 DC 池
        #    池为空/不足 → 采样探测建池；冷启动且池仍不足 → 回退全量随机。
        #    cf-meta-colo 才是实际边缘节点（HKG/SJC/LAX…），cf-meta-country 是客户端所在国。
        target = {c.upper() for c in countries}
        if "CN" in target:  # 港澳台 colo 的 cca2 为 HK/MO/TW，用户常以"中国"泛指
            target |= {"HK", "MO", "TW"}

        def _collect_pools(codes: list):
            """从指定 DC 的池里各取一部分 IP（总量约 TEST_SIZE）；返回 (ips, 缺额codes)。"""
            picked, missing = [], []
            per = max(1, min(50, pools.TEST_SIZE // max(1, len(codes))))
            for c in codes:
                pool = pools.get(c)
                if pool:
                    picked.extend(random.sample(pool, min(per, len(pool))))
                else:
                    missing.append(c)
            return picked, missing

        if colo == "RANDOM":
            # ── 全局随机：全 CF 池随机采样 ──
            self.set_progress("geo", 12, "全局随机采样")
            self.log("全局随机模式：从全 CF IP 池随机采样")
            ips = list(ipdata.sample_cf_ips(sample_n, use_v6, None, [])[0])
            random.shuffle(ips)
        elif colo:
            # ── 指定 DC：单点测速 ──
            self.set_progress("geo", 12, f"加载 {colo} 节点 IP 池")
            self.log(f"指定节点：{geoip.colo_zh(colo)} ({colo})")
            pool = pools.get(colo)
            if pool:
                ips = random.sample(pool, min(pools.TEST_SIZE, len(pool)))
                self.log(f"池命中：从 {colo} 池取 {len(ips)}/{len(pool)} 个 IP")
            else:
                self.log(f"{colo} 池为空，探测建池…", "warn")
                self.set_progress("geo", 14, f"探测 {colo} 节点建池")
                if not self._probe_and_pool([colo], target, use_v6, use_tls):
                    return self._finish_error(f"{colo} 节点探测失败（可能不可达），请选其他节点")
                pool = pools.get(colo)
                ips = random.sample(pool, min(pools.TEST_SIZE, len(pool)))
                self.log(f"{colo} 建池完成：{len(ips)} 个 IP")
        else:
            # ── 按国家组：合并命中国家的 DC 池 ──
            self.set_progress("geo", 12, f"就近过滤：{geoip.countries_zh(countries)}")
            self.log(f"按实际服务节点过滤，目标：{geoip.countries_zh(countries)}")
            cc2colos = geoip.colo_list_by_cc()
            codes = [cd["code"] for cc in sorted(target) for cd in cc2colos.get(cc, [])]
            if not codes:
                return self._finish_error(f"目标国家 {geoip.countries_zh(countries)} 下没有已知节点，请调整国家")
            self.log(f"目标国家共 {len(codes)} 个已知节点（{', '.join(codes[:15])}{'…' if len(codes) > 15 else ''}）")
            ips, missing = _collect_pools(codes)
            if missing and len(ips) < pools.TEST_SIZE:
                self.set_progress("geo", 16, f"补池 {len(missing)} 个节点")
                self.log(f"对 {len(missing)} 个缺额节点采样建池…")
                self._probe_and_pool(missing, target, use_v6, use_tls)
                ips, _ = _collect_pools(codes)
            if len(ips) < 20:
                self.log("就近 IP 池不足（冷启动），回退全量随机采样", "warn")
                ips = list(ipdata.sample_cf_ips(sample_n, use_v6, None, [])[0])
                random.shuffle(ips)
            else:
                self.log(f"就近池就绪：{len(ips)} 个 IP 进入 RTT")

        if not ips:
            return self._finish_error("没有可采样的 Cloudflare IP，请检查网络")

        # 2. ping 预筛选：每 IP 拨号 PING_TIMES 次（TCP+TLS 时延），统计平均时延 + 丢包率
        #    （对齐 CFST 的 tcping 流程：多次拨号取均值、记录丢包）。
        #    相比原高并发 RTT 探测（连接数 = 候选数 × 2~6），这里串行 + 0.1s 间隔，
        #    每候选仅 PING_TIMES 次连接，降低 CF 限流触发概率。
        self.set_progress("rtt", 28, f"ping+丢包 测速 {len(ips)} 个 IP")
        ping_results = []
        pn = len(ips)
        for i, ip in enumerate(ips):
            if self._cancelled():
                return
            self.set_progress("rtt", 28 + int(20 * i / max(1, pn)), f"ping {i+1}/{pn}：{ip}")
            rtt, loss = self._probe_ping_loss(ip, use_tls, PING_TIMES)
            if rtt > 0:
                ping_results.append({"ip": ip, "ping": rtt, "loss": loss})
            if i < pn - 1 and not self._cancelled():
                time.sleep(0.1)
        if self._cancelled():
            return
        if not ping_results:
            return self._finish_error("所有 IP ping 失败（网络可能异常或被拦截）")

        # 过滤：a) 完全丢包（0 可达）已排除；b) 平均时延 > 2× 最佳时延 的淘汰（CFST 相对过滤）
        best_ping = min(r["ping"] for r in ping_results)
        filtered = [r for r in ping_results
                    if r["ping"] <= best_ping * PING_LAT_FACTOR or r["loss"] == 0]
        dropped = len(ping_results) - len(filtered)
        if dropped:
            self.log(f"丢包/高延迟过滤：淘汰 {dropped} 个（时延 > {int(best_ping * PING_LAT_FACTOR)}ms 或丢包严重）")
        ping_results = sorted(filtered, key=lambda r: (r["ping"], r["loss"]))

        # 第二轮候选 = 延迟最低的 top_rtt 个（保留其余作为 0-Mbps 限流时的备用 IP）
        top = ping_results[:top_rtt]
        self.log(f"ping+丢包 预筛选完成：{len(ping_results)}/{pn} 个可达（丢包率最低 {min(r['loss'] for r in ping_results):.0%}），"
                 f"取延迟最低 {len(top)} 个进入下载测速")
        for r in top[:15]:
            self.log(f"  候选 {r['ip']}  {r['ping']}ms · 丢包 {r['loss']:.0%}")

        # 3. 下载测速（第二轮，对齐 CFST 流程）：
        #    - 按 ping 升序逐个串行测速（延迟低的先测，凑够即停 → 快）
        #    - min_speed > 0 时：累计"速度 ≥ 下限"的 IP，凑够 count 个立即停止（CFST -sl 语义）
        #    - min_speed = 0 时：测满 top_rtt 个全部返回（与旧行为一致）
        #    - 下载前再 ping 一次刷新时延，作为 rank 主指标
        #    - 0 Mbps 几乎都是 CF 风控限流（针对出口 IP 全局）：换备用 IP 试一次；
        #      连续 2 个位置都 0 → 60s 全局冷却（让限流窗口完全滑过）
        #    - 成功 IP 回写 DC 池
        self.set_progress("speed", 55, "下载测速")
        sl_note = f"，速度下限 {min_speed:g}Mbps、凑够 {count} 个即停" if min_speed > 0 else ""
        self.log(f"开始下载测速（队列 {len(top)} 个{sl_note}）")
        # 备用 IP 队列：未进入 top 的可达候选（ping 预筛通过），按 ping 升序
        reserve = [pr for pr in ping_results if pr not in top]
        speed_results = []
        need = count  # 仍需凑够的达标 IP 数
        n = len(top)
        consec_zero = 0
        for i, r in enumerate(top):
            if self._cancelled():
                return
            # 达标停止条件（CFST：len(speedSet) == TestCount 时 break）
            if min_speed > 0 and need <= 0:
                self.log(f"已凑够 {count} 个速度达标 IP，提前停止下载测速")
                break
            ip = r["ip"]
            self.set_progress("speed", 55 + int(40 * i / max(1, n)), f"测速 {i+1}/{n}：{ip}")
            # 下载前 ping 一次刷新时延（rank 主指标）
            fresh_ping = self._probe_ping(ip, use_tls)
            r["ping"] = fresh_ping if fresh_ping > 0 else r["ping"]
            res = self._speed_test(r, speed_mb * 1024 * 1024, speed_secs)
            if res["ping"] > 0:
                r["ping"] = res["ping"]
            # 0 Mbps → 换备用 IP 试一次（同 IP 重试无意义，必然再限流）
            if res["mbps"] == 0 and reserve and not self._cancelled():
                alt = reserve.pop(0)
                alt_ip = alt["ip"]
                self.log(f"  {ip} 无数据（CF 限流），换 {alt_ip} 试一次…", "warn")
                r_alt = {"ip": alt_ip, "latency": alt["ping"], "tls": use_tls, "loc": ""}
                res_alt = self._speed_test(r_alt, speed_mb * 1024 * 1024, speed_secs)
                if res_alt["mbps"] > 0:
                    r["ip"] = alt_ip
                    r["ping"] = res_alt["ping"] if res_alt["ping"] > 0 else alt["ping"]
                    r["loss"] = alt["loss"]
                    res = res_alt
            if self._cancelled():
                return
            # 位置：优先 DC 中文名（实际服务节点）
            loc = res.get("dc_zh") or res.get("location") or ""
            res["loc"] = loc
            res["ping"] = r["ping"]
            res["latency"] = r["ping"]
            res["loss"] = r.get("loss", 0)
            tail = f" · {loc}" if loc else ""
            ok_mark = ""
            if min_speed > 0:
                ok_mark = " ✔达标" if res["mbps"] >= min_speed else " ✘未达标"
            self.log(f"  {res['ip']}  ping {r['ping']}ms · 丢包 {r.get('loss', 0):.0%} · {res['mbps']} Mbps{ok_mark}{tail}")
            if min_speed > 0:
                if res["mbps"] >= min_speed:
                    speed_results.append(res)
                    need -= 1
                # 未达标不计入（CFST：speed < MinSpeed 不加入 speedSet）
            else:
                speed_results.append(res)
            # 连续 0 检测：连续 2 个位置都 0 → 60s 全局冷却（限流窗口需 60s+ 才滑过）
            if res["mbps"] > 0:
                consec_zero = 0
            else:
                consec_zero += 1
                if consec_zero == 2:
                    self.log("连续 2 个 IP 无数据（CF 全局限流中），冷却 60s…", "warn")
                    for _ in range(60):
                        if self._cancelled():
                            return
                        time.sleep(1)
                    consec_zero = 0
            # IP 间隔，降低单来源连接密度
            if i < n - 1 and not self._cancelled():
                time.sleep(2.0)
        # 测速成功的 IP 回写 DC 池（供下次直接用）
        ok_ips_by_dc = {}
        for r in speed_results:
            if r["mbps"] > 0 and r.get("dc"):
                ok_ips_by_dc.setdefault(r["dc"].upper(), []).append(r["ip"])
        for dc, ip_list in ok_ips_by_dc.items():
            pools.add(dc, ip_list)

        # 4. 汇总：按 延迟 → 丢包率 → 下载速度 排序（对齐 CFST 的排序维度）
        speed_results.sort(key=lambda r: (
            r.get("ping") or 10**9,          # 延迟升序（主指标）
            r.get("loss") if r.get("loss") is not None else 1.0,  # 丢包率升序
            -r.get("mbps", 0),               # 速度降序（同延迟/丢包时快的优先）
        ))
        out = []
        for r in speed_results[:count]:
            out.append({
                "ip": r["ip"],
                "ping": r.get("ping", 0),
                "latency": r.get("ping", 0),
                "loss": r.get("loss", 0),
                "mbps": r.get("mbps", 0),
                "dc": r.get("dc", ""),
                "dc_zh": r.get("dc_zh", ""),
                "cfRay": r.get("cfRay", ""),
                "location": r.get("loc", ""),
                "tls": r.get("tls", use_tls),
            })

        self.elapsed = int(time.time() - self.start_ts)
        self.result_payload = {
            "count": len(out),
            "elapsed": self.elapsed,
            "ipVer": "v6" if use_v6 else "v4",
            "tls": use_tls,
            "countries": countries,
            "colo": colo or None,
            "minSpeed": min_speed,
            "results": out,
        }
        self.log(f"扫描完成，用时 {self.elapsed} 秒，返回 {len(out)} 个结果"
                 f"（按 延迟/丢包/速度 排序）", "ok")
        self._emit({"running": False, "stage": "done", "pct": 100, "detail": "",
                    "elapsed": self.elapsed,
                    "logs": self.last_state.get("logs", [])})
        self.done.set()

    def _finish_error(self, msg):
        self.log(msg, "error")
        self.result_payload = {"error": msg}
        self._emit({"running": False, "stage": "error", "pct": 100, "detail": msg,
                    "elapsed": int(time.time() - (self.start_ts or time.time())),
                    "logs": self.last_state.get("logs", [])})
        self.done.set()

    # ── ping 预筛选（单次 TCP+TLS 拨号时延，2 次取最好）──

    def _probe_ping(self, ip, use_tls):
        """TCP 拨号(+TLS) 时延（ms），2 次取最好，失败返回 0。
        单候选仅 1~2 次连接，远低于原高并发 RTT 的连接密度。"""
        port = 443 if use_tls else 80
        best_ms = 0
        for attempt in range(2):
            if self._cancelled():
                return max(1, int(best_ms)) if best_ms > 0 else 0
            t0 = time.perf_counter()
            try:
                sock = socket.create_connection((ip, port), timeout=1.5)
            except Exception:
                if best_ms > 0:
                    break
                continue
            try:
                if use_tls:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    sock = ctx.wrap_socket(sock, server_hostname="cloudflare.com")
                if best_ms == 0 or (time.perf_counter() - t0) * 1000 < best_ms:
                    best_ms = (time.perf_counter() - t0) * 1000
            except Exception:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
            if attempt == 0 and not self._cancelled():
                time.sleep(0.05)
        return max(1, int(best_ms)) if best_ms > 0 else 0

    def _probe_ping_loss(self, ip, use_tls, times=PING_TIMES):
        """ping + 丢包率探测（对齐 CFST tcping：times 次拨号）。

        返回 (avg_ms, loss)：
          avg_ms — 成功连接的 TCP+TLS 握手平均时延（ms），0 = 全部失败；
          loss   — 丢包率（0.0~1.0），= 失败次数 / times。
        串行拨号 + 次间 0.05s 间隔，降低触发 CF 限流的连接密度。
        """
        port = 443 if use_tls else 80
        succ = 0
        total_ms = 0.0
        for attempt in range(times):
            if self._cancelled():
                break
            t0 = time.perf_counter()
            try:
                sock = socket.create_connection((ip, port), timeout=1.5)
            except Exception:
                sock = None
            if sock is None:
                continue
            try:
                if use_tls:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    sock = ctx.wrap_socket(sock, server_hostname="cloudflare.com")
                succ += 1
                total_ms += (time.perf_counter() - t0) * 1000
            except Exception:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
            if attempt < times - 1 and not self._cancelled():
                time.sleep(0.05)
        loss = (times - succ) / max(1, times)
        avg = int(round(total_ms / succ)) if succ > 0 else 0
        return avg, loss

    # ── 建池 ──

    def _probe_and_pool(self, codes: list, target_cc: set, use_v6: bool, use_tls: bool) -> bool:
        """采样候选 → 并发探测 cf-meta-colo → 归入命中 DC 的 IP 池。"""
        need = {c.upper(): max(1, pools.POOL_SIZE - pools.size(c)) for c in codes if pools.size(c) < pools.POOL_SIZE}
        if not need:
            return True
        total_need = sum(need.values())
        sample_n = min(1500, max(200, total_need * 4))
        pool = list(ipdata.sample_cf_ips(sample_n, use_v6, None, [])[0])
        random.shuffle(pool)
        probe_n = min(200, len(pool))
        self.log(f"采样 {len(pool)} 个候选，探测 {probe_n} 个实际服务节点（目标 {len(need)} 个节点，缺额 {total_need}）")

        def probe(ip):
            cc, colo, city = ipdata.probe_location(ip, use_tls, timeout=4)
            return ip, cc, colo

        done = 0
        lock = threading.Lock()
        with _cf.ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(probe, ip): ip for ip in pool[:probe_n]}
            for f in _cf.as_completed(futs):
                if self._cancelled():
                    break
                ip, cc, colo = f.result()
                done += 1
                if done % 25 == 0 or done == probe_n:
                    self.set_progress("geo", 12 + int(16 * done / probe_n),
                                      f"探测实际服务节点 {done}/{probe_n}")
                if not colo:
                    continue
                cc_colo = geoip.colo_country(colo)
                if not cc_colo:
                    continue
                if cc_colo not in target_cc and colo.upper() not in need:
                    continue
                with lock:
                    if pools.size(colo) < pools.POOL_SIZE:
                        pools.add(colo, [ip])
        added = {c: pools.size(c) for c in need}
        self.log(f"建池完成：{', '.join(f'{c}={n}' for c, n in sorted(added.items()))}")
        return any(added.values())

    # ── 带宽测速（内置 ping：连接时延为 rank 主指标）──

    def _speed_test(self, r, speed_bytes, speed_secs):
        """单次连接完成 ping（TCP+TLS 时延）+ 下载测速。
        返回 {ip, port, ping, mbps, dc, dc_zh, cfRay, location}。
        ping: TCP+TLS 握手到可发送的毫秒数（≈ RTT + TLS 开销），失败为 0。
        mbps: 下载峰值（可能为 0 = CF 限流，仅作速度参考）。"""
        ip = r["ip"]
        use_tls = r.get("tls", True)
        host = "speed.cloudflare.com"
        port = 443 if use_tls else 80
        result = {"ip": ip, "port": port, "ping": 0, "latency": 0, "mbps": 0,
                  "dc": "", "dc_zh": "", "cfRay": "", "location": ""}
        conn = None
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((ip, port), timeout=3)
            if use_tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = ctx.wrap_socket(sock, server_hostname=host)
            else:
                conn = sock
            # 连接时延 = ping（TCP + TLS 握手总耗时）
            result["ping"] = result["latency"] = max(1, int((time.perf_counter() - t0) * 1000))
            conn.settimeout(speed_secs + 8)

            req = (f"GET /__down?bytes={speed_bytes} HTTP/1.1\r\n"
                   f"Host: {host}\r\nUser-Agent: Mozilla/5.0 (FastCF)\r\n"
                   f"Connection: close\r\n\r\n").encode()
            conn.sendall(req)

            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                head += chunk
            if b"\r\n\r\n" not in head:
                return result
            head_str = head.split(b"\r\n\r\n", 1)[0].decode("latin-1", "ignore")
            # 提取 CF 头：CF-RAY（DC 代码）+ cf-meta-*（实际服务位置）
            meta = {}
            for line in head_str.split("\r\n"):
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "cf-ray":
                    result["cfRay"] = v
                    parts = v.split("-")
                    if len(parts) >= 2:
                        result["dc"] = parts[-1].strip()
                        result["dc_zh"] = geoip.colo_zh(result["dc"])
                elif k.startswith("cf-meta-"):
                    meta[k] = v
                elif k in ("country", "city", "colo"):
                    # speed.cloudflare.com 也返回这些头
                    meta[f"cf-meta-{k}"] = v
            # 用 CF 返回的实际服务位置（比 ip2region 注册归属更准确）
            loc_parts = []
            country_code = meta.get("cf-meta-country", "")
            city = meta.get("cf-meta-city", "")
            if country_code:
                loc_parts.append(geoip.country_zh(country_code))
            if city and city not in ("0", "N/A"):
                loc_parts.append(city)
            if loc_parts:
                result["location"] = "·".join(loc_parts)
            body = head.split(b"\r\n\r\n", 1)[1]

            peak_bps = 0.0
            win_bytes, win_start = 0, time.time()
            global_start = time.time()
            while time.time() - global_start < speed_secs:
                if self._cancelled():
                    break
                if not body:
                    try:
                        body = conn.recv(65536)
                    except socket.timeout:
                        break
                    except Exception:
                        break
                    if not body:
                        break
                win_bytes += len(body)
                body = b""
                now = time.time()
                if now - win_start >= 1.0:
                    bps = win_bytes * 8 / (now - win_start)
                    if bps > peak_bps:
                        peak_bps = bps
                    win_bytes, win_start = 0, now
            result["mbps"] = int(peak_bps / 1_000_000)
        except Exception as e:
            self.log(f"  测速 {ip} 异常：{e}", "warn")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return result
