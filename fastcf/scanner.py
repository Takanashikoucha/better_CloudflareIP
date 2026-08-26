# -*- coding: utf-8 -*-
"""测速引擎：取池/建池 → ping 预筛选 → 逐节点下载测速 → 按 ping 排名。

所有网络 I/O 均为直连 socket（模块导入时已清除代理环境变量），
不经过任何系统代理。
"""
import random
import socket
import ssl
import threading
import time

from . import filler, geoip, ipdata, pools

# 每轮扫描最多保留的 RTT 候选数（默认值；可被 params["top_rtt"] 覆盖）
TOP_RTT = 10
# ping 预筛选：每个候选 N 次拨号（对齐 CFST 的 PingTimes=4）
PING_TIMES = 4
# ping 预筛选：平均延迟超过 2× 最小时延 的候选直接淘汰
PING_LAT_FACTOR = 2.0


class Scanner:
    """一次扫描的编排器。"""

    def __init__(self, params: dict):
        self.p = params
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.subscribers: list = []
        import queue
        self.queue = queue
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
            logs = list(self.last_state.get("logs", [])) if self.last_state else []
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

    def subscribe(self):
        q = self.queue.Queue(maxsize=200)
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
        use_v6 = p.get("ipVer") == "v6"
        use_tls = bool(p.get("tls", True))
        port = 80 if not use_tls else 443
        speed_secs = max(3, min(60, float(p.get("speedSecs", 8))))
        speed_mb = max(10, min(1000, int(p.get("speedMB", 50))))
        count = max(1, min(10, int(p.get("count", 5))))
        countries = [c for c in p.get("countries", geoip.DEFAULT_NEARBY)]
        colo = (p.get("colo") or "").strip().upper()  # ""=自动就近, "RANDOM"=全局随机, 其他=指定 DC
        top_rtt = max(3, min(30, int(p.get("top_rtt", TOP_RTT))))
        min_speed = max(0.0, min(10000.0, float(p.get("minSpeed", 0) or 0)))

        self.set_progress("prepare", 5, "准备中")
        self.log(f"开始扫描：{'IPv6' if use_v6 else 'IPv4'} | TLS={'开' if use_tls else '关'} | "
                 f"测速={speed_secs:.0f}s/{speed_mb}MB"
                 f"{' | 速度下限 ' + format(min_speed, 'g') + 'Mbps' if min_speed > 0 else ''} | 返回 Top{count}")

        filler.scan_started()
        try:
            self._run_body(use_v6, use_tls, port, speed_secs, speed_mb, count,
                           countries, colo, top_rtt, min_speed)
        finally:
            filler.scan_finished()

    def _run_body(self, use_v6, use_tls, port, speed_secs, speed_mb, count,
                  countries, colo, top_rtt, min_speed):
        # 1. 构建候选 IP 集
        #    a) 指定 colo → 只用该 DC 的池（池空则同步建池，建完仍空 → 报错）
        #    b) "RANDOM" → 全量随机采样
        #    c) 按国家组 → 合并命中国家的各 DC 池，缺额由后台 filler 补
        target = {c.upper() for c in countries}
        if "CN" in target:  # 港澳台 colo 的 cca2 为 HK/MO/TW
            target |= {"HK", "MO", "TW"}

        if colo == "RANDOM":
            self.set_progress("geo", 12, "全局随机采样")
            self.log("全局随机模式：从全 CF IP 池随机采样")
            ips = list(ipdata.sample_cf_ips(150, use_v6))
            random.shuffle(ips)
        elif colo:
            # ── 指定 DC：单点测速 ──
            self.set_progress("geo", 12, f"加载 {colo} 节点 IP 池")
            self.log(f"指定节点：{geoip.colo_zh(colo)} ({colo})")
            pool = pools.get(colo)
            if not pool or pools.expired():
                if pools.expired() and pool:
                    self.log(f"{colo} 池已过期，先重新探测验证…", "warn")
                else:
                    self.log(f"{colo} 池为空，探测建池…", "warn")
                self.set_progress("geo", 14, f"探测 {colo} 节点建池")
                pools.refill([colo], use_v6, use_tls, max_probes=200,
                             stop=self._cancelled, log=lambda m: self.log(m))
                pool = pools.get(colo)
                if not pool:
                    return self._finish_error(
                        f"{colo} 节点池为空且建池失败（该节点可能不可达），请换其他节点或稍后重试")
                self.log(f"{colo} 建池完成：{len(pool)} 个 IP")
            ips = random.sample(pool, min(pools.TEST_SIZE, len(pool)))
            self.log(f"从 {colo} 池取 {len(ips)}/{len(pool)} 个 IP")
        else:
            # ── 按国家组 ──
            self.set_progress("geo", 12, f"就近过滤：{geoip.countries_zh(countries)}")
            cc2colos = geoip.colo_list_by_cc()
            codes = [cd["code"] for cc in sorted(target) for cd in cc2colos.get(cc, [])]
            if not codes:
                return self._finish_error(f"目标国家 {geoip.countries_zh(countries)} 下没有已知节点，请调整国家")
            self.log(f"目标国家共 {len(codes)} 个已知节点（{', '.join(codes[:15])}{'…' if len(codes) > 15 else ''}）")
            picked, missing = [], []
            for c in codes:
                pool = pools.get(c)
                if pool:
                    picked.extend(random.sample(pool, min(50, len(pool))))
                else:
                    missing.append(c)
            if missing:
                self.log(f"{len(missing)} 个节点池为空，后台填充中（本次先用现有 {len(picked)} 个）")
                filler.start().wake_for(missing)
            if len(picked) < 20:
                self.log("就近 IP 池不足（冷启动），回退全量随机采样", "warn")
                ips = list(ipdata.sample_cf_ips(150, use_v6))
                random.shuffle(ips)
            else:
                ips = picked
                self.log(f"就近池就绪：{len(ips)} 个 IP 进入 RTT")

        if not ips:
            return self._finish_error("没有可测速的 Cloudflare IP，请检查网络")

        # 2. ping 预筛选：每 IP 拨号 PING_TIMES 次（TCP+TLS 时延），统计平均时延 + 丢包率
        #    并发探测（线程池），避免串行 150×4×1.5s 超时堆积
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.set_progress("rtt", 28, f"ping+丢包 测速 {len(ips)} 个 IP（并发）")
        ping_results = []
        pn = len(ips)
        done_count = 0

        def _ping_one(ip):
            if self._cancelled():
                return None
            rtt, loss = self._probe_ping_loss(ip, use_tls, port, PING_TIMES)
            return (ip, rtt, loss)

        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(_ping_one, ip): ip for ip in ips}
            for fut in as_completed(futs):
                if self._cancelled():
                    ex.shutdown(wait=False)
                    return
                try:
                    r = fut.result()
                except Exception:
                    r = None
                done_count += 1
                self.set_progress("rtt", 28 + int(20 * done_count / pn), f"ping {done_count}/{pn}")
                if r and r[1] > 0:
                    ping_results.append({"ip": r[0], "ping": r[1], "loss": r[2]})
        if not ping_results:
            return self._finish_error("所有 IP ping 失败（网络可能异常或被拦截）")

        # 过滤：平均时延 > 2× 最佳时延 的淘汰（丢包严重的保留最低延迟者除外）
        best_ping = min(r["ping"] for r in ping_results)
        filtered = [r for r in ping_results
                    if r["ping"] <= best_ping * PING_LAT_FACTOR or r["loss"] == 0]
        dropped = len(ping_results) - len(filtered)
        if dropped:
            self.log(f"丢包/高延迟过滤：淘汰 {dropped} 个（时延 > {int(best_ping * PING_LAT_FACTOR)}ms 或丢包严重）")
        ping_results = sorted(filtered, key=lambda r: (r["ping"], r["loss"]))

        # 第二轮候选 = 延迟最低的 top_rtt 个；其余作为 0-Mbps 时的备用 IP
        top = ping_results[:top_rtt]
        self.log(f"ping+丢包 预筛选完成：{len(ping_results)}/{pn} 个可达"
                 f"（最低丢包 {min(r['loss'] for r in ping_results):.0%}），"
                 f"取延迟最低 {len(top)} 个进入下载测速")
        for r in top[:15]:
            self.log(f"  候选 {r['ip']}  {r['ping']}ms · 丢包 {r['loss']:.0%}")

        # 3. 下载测速（按 ping 升序逐个串行）
        #    - 0Mbps 几乎都是 CF 风控限流：换备用 IP 重试（备用用尽则记录 0 继续）
        #    - min_speed > 0 时：凑够 count 个达标 IP 即停
        #    - 成功 IP 回写其实际 DC 池
        self.set_progress("speed", 55, "下载测速")
        sl_note = f"，速度下限 {min_speed:g}Mbps、凑够 {count} 个即停" if min_speed > 0 else ""
        self.log(f"开始下载测速（队列 {len(top)} 个{sl_note}）")
        reserve = [pr for pr in ping_results if pr not in top]
        speed_results = []
        need = count  # 仍需凑够的达标 IP 数
        n = len(top)
        for i, r in enumerate(top):
            if self._cancelled():
                return
            if min_speed > 0 and need <= 0:
                self.log(f"已凑够 {count} 个速度达标 IP，提前停止下载测速")
                break
            ip = r["ip"]
            self.set_progress("speed", 55 + int(40 * i / max(1, n)), f"测速 {i+1}/{n}：{ip}")
            # 下载前 ping 一次刷新时延（rank 主指标）
            fresh_ping = self._probe_ping(ip, use_tls, port)
            r["ping"] = fresh_ping if fresh_ping > 0 else r["ping"]
            res = self._speed_test(ip, use_tls, port, speed_mb * 1024 * 1024, speed_secs)
            if res["ping"] > 0:
                r["ping"] = res["ping"]
            if self._cancelled():
                break
            # 0Mbps → 换备用 IP 试一次（同 IP 重试无意义，必然再限流）
            if res["mbps"] == 0 and reserve and not self._cancelled():
                alt = reserve.pop(0)
                self.log(f"  {ip} 无数据（CF 限流），换 {alt['ip']} 试一次…", "warn")
                res_alt = self._speed_test(alt["ip"], use_tls, port, speed_mb * 1024 * 1024, speed_secs)
                if res_alt["mbps"] > 0:
                    r["ip"], r["ping"], r["loss"] = alt["ip"], \
                        res_alt["ping"] if res_alt["ping"] > 0 else alt["ping"], alt["loss"]
                    res = res_alt
            if self._cancelled():
                return
            # 位置：CF 返回的实际服务节点（比注册归属更准确）
            loc = res.get("dc_zh") or res.get("location") or ""
            res["loc"] = loc
            res["ping"] = r["ping"]
            res["latency"] = r["ping"]
            res["loss"] = r.get("loss", 0)
            res["port"] = port
            tail = f" · {loc}" if loc else ""
            ok_mark = ""
            if min_speed > 0:
                ok_mark = " ✔达标" if res["mbps"] >= min_speed else " ✘未达标"
            self.log(f"  {res['ip']}  ping {r['ping']}ms · 丢包 {r.get('loss', 0):.0%} · "
                     f"{res['mbps']} Mbps{ok_mark}{tail}")
            if min_speed > 0:
                if res["mbps"] >= min_speed:
                    speed_results.append(res)
                    need -= 1
            else:
                speed_results.append(res)
            # IP 间隔，降低单来源连接密度
            if i < n - 1 and not self._cancelled():
                time.sleep(2.0)

        # 测速成功的 IP 回写其实际 DC 池；顺带唤醒 filler 补缺额
        ok_by_dc = {}
        for r in speed_results:
            if r["mbps"] > 0 and r.get("dc"):
                ok_by_dc.setdefault(r["dc"].upper(), []).append(r["ip"])
        for dc, ip_list in ok_by_dc.items():
            pools.add(dc, ip_list)
        if ok_by_dc:
            filler.start().wake_for(list(ok_by_dc.keys()))

        # 4. 汇总：按 延迟 → 丢包率 → 下载速度 排序
        speed_results.sort(key=lambda r: (
            r.get("ping") or 10**9,
            r.get("loss") if r.get("loss") is not None else 1.0,
            -r.get("mbps", 0),
        ))
        out = []
        for r in speed_results[:count]:
            out.append({
                "ip": r["ip"],
                "ping": r.get("ping", 0),
                "latency": r.get("ping", 0),
                "loss": r.get("loss", 0),
                "mbps": r.get("mbps", 0),
                "port": port,
                "dc": r.get("dc", ""),
                "dc_zh": r.get("dc_zh", ""),
                "cfRay": r.get("cfRay", ""),
                "location": r.get("loc", ""),
                "tls": use_tls,
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
        self.log(f"扫描完成，用时 {self.elapsed} 秒，返回 {len(out)} 个结果（按 延迟/丢包/速度 排序）", "ok")
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

    # ── ping 预筛选 ──

    def _probe_ping(self, ip, use_tls, port):
        """TCP(+TLS) 拨号时延（ms），2 次取最好，失败返回 0。"""
        best_ms = 0
        for attempt in range(2):
            if self._cancelled():
                break
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

    def _probe_ping_loss(self, ip, use_tls, port, times=PING_TIMES):
        """ping + 丢包率探测：times 次拨号。
        返回 (avg_ms, loss)：成功连接的 TCP+TLS 握手平均时延；失败次数/times。"""
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

    # ── 带宽测速 ──

    def _speed_test(self, ip, use_tls, port, speed_bytes, speed_secs):
        """单次连接完成 ping（TCP+TLS 时延）+ 下载测速。
        返回 {ip, port, ping, mbps, dc, dc_zh, cfRay, location}。
        mbps 可能为 0 = CF 限流。"""
        host = "speed.cloudflare.com"
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
                    meta[f"cf-meta-{k}"] = v
            # CF 返回的实际服务位置
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
