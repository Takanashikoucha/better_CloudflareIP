# -*- coding: utf-8 -*-
"""测速引擎（v2 设计）。

固定口径：IPv4 · 443/TLS · 结果 5 个。

流程（指定 DC 与全局随机共用，只差候选来源）：
  A. 候选集：
     - 指定 DC：取该 DC 池（池为空 → 直接回退随机，不自动建池）
     - 全局随机：从全 CF v4 段（TYOYO1/CF-ASN 全量段）随机采样「随机 IP 数量」个
  B. ping 预筛：ICMP ping（`ping -c 4 -W 2`）并发 200，
     平均时延 + 丢包率；丢包 ≥75% 淘汰并从所属 DC 池剔除；
     时延 > 2× 最佳时延 淘汰（零丢包者豁免）
  C. 下载测速：按延迟升序串行，443/TLS 下载
     - 随机 IP 测速前先探测 cf-meta-colo 确认实际 DC 并入池
     - 队列 = ping 预筛通过的全部候选，凑够 5 个达标 → 停止
     - 未达标（0Mbps/限流等）→ 继续测队列中下一个候选
  D. 回退：指定 DC 模式候选不足（池空 / 达标数 < 5）→ 进入随机模式
     再跑一遍 B+C，把总数补齐到 5
  E. 汇总：按 延迟 → 丢包 → 速度 排序，输出 5 个
     测速成功（>0Mbps）的 IP 回写其实际 DC 池
"""
import random
import re
import socket
import ssl
import subprocess
import threading
import time

from . import geoip, ipdata, pools

RESULT_COUNT = 5        # 固定返回 5 个结果
PING_TIMES = 4          # 每个 IP 发 4 个 ICMP 包
PING_TIMEOUT = 2.0      # 单包超时（秒）
PING_WORKERS = 200      # ping 并发度
PING_LAT_FACTOR = 2.0   # 平均时延 > 2× 最佳时延 淘汰
LOSS_CUTOFF = 0.75      # 丢包 ≥75% 淘汰 + 剔出池


def icmp_ping(ip: str, times: int = PING_TIMES, timeout: float = PING_TIMEOUT):
    """ICMP ping（系统 `ping` 命令）。返回 (avg_ms, loss)；不可达返回 (0, 1.0)。"""
    try:
        out = subprocess.run(
            ["ping", "-c", str(times), "-W", str(int(timeout)), ip],
            capture_output=True, text=True, timeout=times * timeout + 10)
    except Exception:
        return 0, 1.0
    m = re.search(r"(\d+(?:\.\d+)?)%\s*packet\s*loss", out.stdout or "")
    loss = 1.0
    if m:
        loss = float(m.group(1)) / 100.0
    # 平均时延："rtt min/avg/max/mdev = 12.1/14.3/16.9/1.2 ms"（中文/英文输出同形）
    r = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+", out.stdout or "")
    avg_ms = float(r.group(1)) if r else 0.0
    if avg_ms > 0:
        avg_ms = max(1, round(avg_ms))  # 亚毫秒时延（回环）向上取整到 1ms
    else:
        avg_ms = 0
    if loss >= 1.0:
        return 0, 1.0
    return avg_ms, loss


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
        speed_secs = max(3, min(60, float(p.get("speedSecs", 8))))
        speed_mb = max(10, min(1000, int(p.get("speedMB", 50))))
        min_speed = max(0.0, min(10000.0, float(p.get("minSpeed", 0) or 0)))
        random_count = max(10, min(2000, int(p.get("randomCount", 150))))
        mode = (p.get("mode") or "").strip().upper()
        colo = (p.get("colo") or "").strip().upper()
        if mode == "DC" and not re.fullmatch(r"[A-Z]{3}", colo):
            self._finish_error("指定 DC 模式需要有效的节点代码（如 HKG）")
            return

        self.set_progress("prepare", 5, "准备中")
        self.log(f"开始扫描：IPv4 · 443/TLS · 返回 Top{RESULT_COUNT}"
                 f" | 测速={speed_secs:.0f}s/{speed_mb}MB"
                 f"{' | 速度下限 ' + format(min_speed, 'g') + 'Mbps' if min_speed > 0 else ''}"
                 f" | 模式={'指定 DC ' + colo if mode == 'DC' else '全局随机'}"
                 + (f"（随机 {random_count} 个 IP）" if mode != "DC" else ""))

        speed_results = []
        fallback_used = False

        # ── 第一步：指定 DC 模式 ──
        if mode == "DC":
            self.log(f"指定节点：{geoip.colo_zh(colo)} ({colo})")
            pool = pools.get(colo)
            if not pool:
                self.log(f"{colo} IP 池为空 → 回退全局随机模式", "warn")
                fallback_used = True
            else:
                # 池过期：事件性重新探测（前台同步，非后台）
                if pools.expired(colo):
                    self.log(f"{colo} 池已过期，事件性重新探测 {len(pool)} 个 IP…", "warn")
                    self.set_progress("revalidate", 10, f"重验 {colo} 池")
                    pool = self._revalidate_pool(colo, pool)
                ips = random.sample(pool, min(len(pool), pools.TEST_SIZE))
                self.log(f"从 {colo} 池取 {len(ips)}/{len(pool)} 个 IP")
                speed_results = self._speed_phase(ips, speed_secs, speed_mb, min_speed,
                                                  need=RESULT_COUNT, dc_hint=colo)
                if self._cancelled():
                    return
                if len(speed_results) < RESULT_COUNT:
                    self.log(f"指定 DC 达标 {len(speed_results)} 个 < {RESULT_COUNT}"
                             f" → 回退全局随机补到 {RESULT_COUNT} 个", "warn")
                    fallback_used = True

        # ── 第二步：全局随机模式（指定模式不足时的回退，或用户直接选随机）──
        if mode != "DC" or fallback_used:
            if self._cancelled():
                return
            existing = {r["ip"] for r in speed_results}
            need = RESULT_COUNT - len(existing)
            self.set_progress("geo", 12, f"全局随机采样 {random_count} 个 IP")
            self.log(f"全局随机模式：从全 CF IP 段随机采样 {random_count} 个"
                     f"{'（补齐 ' + str(need) + ' 个）' if need < RESULT_COUNT else ''}")
            ips = [ip for ip in ipdata.sample_cf_ips(random_count, use_v6=False)
                   if ip not in existing]
            random.shuffle(ips)
            more = self._speed_phase(ips, speed_secs, speed_mb, min_speed,
                                     need=need, random_pool=True)
            speed_results.extend(more)

        if self._cancelled():
            return
        if not speed_results:
            return self._finish_error("没有可测速的 Cloudflare IP（网络异常或被拦截），请检查网络")

        # ── 汇总 ──
        # 成功（>0Mbps）的 IP 回写其实际 DC 池
        ok_by_dc = {}
        for r in speed_results:
            if r["mbps"] > 0 and r.get("dc"):
                ok_by_dc.setdefault(r["dc"].upper(), []).append(r["ip"])
        for dc, ip_list in ok_by_dc.items():
            pools.add(dc, ip_list)
        if ok_by_dc:
            self.log(f"回写池：{', '.join(f'{dc} +{len(v)}' for dc, v in ok_by_dc.items())}")

        speed_results.sort(key=lambda r: (
            r.get("ping") or 10**9,
            r.get("loss") if r.get("loss") is not None else 1.0,
            -r.get("mbps", 0),
        ))
        out = []
        for r in speed_results[:RESULT_COUNT]:
            out.append({
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
        self.result_payload = {
            "count": len(out),
            "elapsed": self.elapsed,
            "ipVer": "v4",
            "tls": True,
            "mode": "DC" if mode == "DC" and not fallback_used else
                    ("DC+随机" if mode == "DC" and fallback_used else "RANDOM"),
            "colo": colo or None,
            "randomCount": random_count,
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

    # ── 事件性池重验（池过期且被指定 DC 扫描用到时，前台同步执行）──

    def _revalidate_pool(self, colo: str, ips: list) -> list:
        """并发 ping 池内 IP：成功 → 刷新时间戳；丢包 ≥75% → 剔除。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        alive, bad = [], []
        done = 0
        with ThreadPoolExecutor(max_workers=PING_WORKERS) as ex:
            futs = {ex.submit(icmp_ping, ip): ip for ip in ips}
            for fut in as_completed(futs):
                if self._cancelled():
                    ex.shutdown(wait=False)
                    return []
                ip = futs[fut]
                try:
                    r = fut.result()
                except Exception:
                    r = (0, 1.0)
                done += 1
                self.set_progress("revalidate", 10 + int(6 * done / len(ips)),
                                  f"重验 {done}/{len(ips)}")
                if r[1] >= LOSS_CUTOFF:
                    bad.append(ip)
                else:
                    alive.append(ip)
        if bad:
            pools.remove(colo, bad)
            self.log(f"重验：{colo} 剔除 {len(bad)} 个失效 IP，保留 {len(alive)} 个")
        else:
            self.log(f"重验：{colo} 全部 {len(alive)} 个 IP 有效")
        pools.touch(colo)
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

        # ── B. ping 预筛（ICMP，并发 200）──
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.set_progress("rtt", 20, f"ICMP ping {pn} 个 IP（并发 {PING_WORKERS}）")
        ping_results = []
        done_count = 0
        with ThreadPoolExecutor(max_workers=PING_WORKERS) as ex:
            futs = {ex.submit(icmp_ping, ip): ip for ip in ips}
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
                self.set_progress("rtt", 20 + int(15 * done_count / pn),
                                  f"ping {done_count}/{pn}")
                if avg_ms > 0:
                    ping_results.append({"ip": ip, "ping": avg_ms, "loss": loss})
        if not ping_results:
            self.log("所有 IP ping 失败（网络异常或被拦截）", "error")
            return []

        # 丢包 ≥75%：淘汰 + 从所属 DC 池剔除
        bad_ips = [r["ip"] for r in ping_results if r["loss"] >= LOSS_CUTOFF]
        if bad_ips:
            removed_any = False
            for dc in pools.all_codes():
                pool_ips = set(pools.get(dc))
                bad_in_dc = [ip for ip in bad_ips if ip in pool_ips]
                if bad_in_dc:
                    pools.remove(dc, bad_in_dc)
                    removed_any = True
            if removed_any:
                self.log(f"ping 丢包 ≥{LOSS_CUTOFF:.0%}：从池中剔除 {len(bad_ips)} 个失效 IP")
        ping_results = [r for r in ping_results if r["loss"] < LOSS_CUTOFF]

        # 时延 > 2× 最佳时延 淘汰（零丢包者豁免）
        if ping_results:
            best_ping = min(r["ping"] for r in ping_results)
            filtered = [r for r in ping_results
                        if r["ping"] <= best_ping * PING_LAT_FACTOR or r["loss"] == 0]
            dropped = len(ping_results) - len(filtered)
            if dropped:
                self.log(f"时延过滤：淘汰 {dropped} 个（时延 > {int(best_ping * PING_LAT_FACTOR)}ms）")
            ping_results = filtered

        ping_results.sort(key=lambda r: (r["ping"], r["loss"]))
        queue = list(ping_results)  # 下载队列 = 全部预筛通过候选，按延迟升序
        self.log(f"ping 预筛完成：{len(ping_results)}/{pn} 个可达"
                 f"（最低丢包 {min(r['loss'] for r in ping_results):.0%}），"
                 f"全部 {len(queue)} 个进入下载测速（凑够 {need} 个达标即停）")
        for r in queue[:15]:
            self.log(f"  候选 {r['ip']}  ping {r['ping']}ms · 丢包 {r['loss']:.0%}")
        if len(queue) > 15:
            self.log(f"  …（其余 {len(queue) - 15} 个候选略）")

        # ── C. 下载测速（443/TLS，按延迟升序串行，队列 = 全部预筛通过候选）──
        self.set_progress("speed", 45, "下载测速")
        sl_note = (f"，速度下限 {min_speed:g}Mbps" if min_speed > 0 else "") + f"，凑够 {need} 个即停"
        self.log(f"开始下载测速（队列 {len(queue)} 个{sl_note}）")
        results = []
        for i, r in enumerate(queue):
            if self._cancelled():
                return results
            if len(results) >= need:
                break
            ip = r["ip"]
            self.set_progress("speed", 45 + int(45 * i / max(1, len(queue))),
                              f"测速 {i + 1}/{len(queue)}：{ip}")
            # 随机 IP：测速前探测实际服务节点，确认 DC 并入池
            if random_pool:
                self.set_progress("speed", 45 + int(45 * i / max(1, len(queue))),
                                  f"探测 {ip} 实际节点…")
                _cc, colo_hit, _city = ipdata.probe_location(ip, use_tls=True, timeout=4)
                if colo_hit:
                    pools.add(colo_hit.upper(), [ip], save=True)
                    self.log(f"  {ip} 实际节点 {geoip.colo_zh(colo_hit)} ({colo_hit})，已入池")
                else:
                    self.log(f"  {ip} 探测未读到实际节点，继续测速", "warn")
            res = self._speed_test(ip, speed_mb * 1024 * 1024, speed_secs)
            if self._cancelled():
                return results
            loc = res.get("dc_zh") or res.get("location") or ""
            res["loc"] = loc
            res["ping"] = r["ping"]
            res["loss"] = r.get("loss", 0)
            res["port"] = 443
            ok = res["mbps"] > 0 if min_speed == 0 else res["mbps"] >= min_speed
            mark = " ✔达标" if ok else " ✘未达标"
            self.log(f"  {res['ip']}  ping {r['ping']}ms · 丢包 {r.get('loss', 0):.0%} · "
                     f"{res['mbps']} Mbps{mark} · {loc}")
            if ok:
                results.append(res)
        return results

    # ── 单次下载测速（443/TLS）──

    def _speed_test(self, ip: str, speed_bytes: int, speed_secs: float) -> dict:
        """单次连接完成下载测速。返回 {ip, port, ping, mbps, dc, dc_zh, cfRay, location}。
        mbps 可能为 0 = CF 限流。ping 为本次连接 TCP+TLS 全程时延（仅记录，排名以 ICMP 为准）。"""
        host = "speed.cloudflare.com"
        result = {"ip": ip, "port": 443, "ping": 0, "latency": 0, "mbps": 0,
                  "dc": "", "dc_zh": "", "cfRay": "", "location": ""}
        conn = None
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((ip, 443), timeout=3)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = ctx.wrap_socket(sock, server_hostname=host)
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
                    except (socket.timeout, Exception):
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
