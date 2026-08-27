# -*- coding: utf-8 -*-
"""HTTP 服务：静态 Web UI + JSON API + SSE 实时日志流。"""
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, exports, geoip, ipdata, pools
from .scanner import Scanner

WEB_DIR = Path(__file__).parent / "web"
HISTORY_FILE = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf"))) / "history.json"


# ── 历史 ──

def load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def save_history(h: list):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=2))


def add_history(payload: dict, params: dict):
    h = load_history()
    entry = {
        "id": int(time.time() * 1000),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **payload,
        "params": params,
    }
    h.insert(0, entry)
    save_history(h[:50])


# ── 扫描管理 ──

class ScanManager:
    def __init__(self):
        self.scanner: Scanner | None = None
        self.lock = threading.Lock()
        self.last_result: dict | None = None
        self.last_params: dict | None = None

    def start(self, params: dict):
        with self.lock:
            if self.scanner and self.scanner.last_state and self.scanner.last_state.get("running"):
                return False, "扫描正在进行中"
            self.scanner = Scanner(params)
            self.last_result = None
        t = threading.Thread(target=self._run, args=(self.scanner, params), daemon=True)
        t.start()
        return True, ""

    def _run(self, sc: Scanner, params: dict):
        try:
            sc.run()
        except Exception as e:
            sc._finish_error(f"扫描异常：{e}")
        if sc.result_payload and "error" not in sc.result_payload:
            with self.lock:
                self.last_result = sc.result_payload
                self.last_params = params
            add_history(sc.result_payload, params)

    def cancel(self):
        with self.lock:
            if self.scanner:
                self.scanner.cancel()

    @property
    def running(self):
        with self.lock:
            return bool(self.scanner and self.scanner.last_state
                        and self.scanner.last_state.get("running"))


manager = ScanManager()


def _read_html():
    # 页脚版本号与包版本保持单一来源
    return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace(
        "v__VERSION__", f"v{__version__}")


class FastCFHandler(BaseHTTPRequestHandler):
    server_version = f"FastCF/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ── 工具 ──

    def _send(self, body: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

    def _body_json(self) -> dict:
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    # ── GET ──

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(_read_html().encode(), "text/html; charset=utf-8")
        elif path in ("/app.js", "/style.css"):
            f = WEB_DIR / path.lstrip("/")
            if f.exists():
                ctype = "text/javascript" if f.suffix == ".js" else "text/css"
                self._send(f.read_bytes(), f"{ctype}; charset=utf-8")
            else:
                self._json({"error": "not found"}, 404)
        elif path == "/api/status":
            with manager.lock:
                sc = manager.scanner
                result = manager.last_result
            out = {}
            if sc is not None and sc.result_payload and "error" in sc.result_payload:
                out["error"] = sc.result_payload["error"]
            if result:
                out["result"] = result
                out["params"] = manager.last_params
            out["running"] = manager.running
            self._json(out)
        elif path == "/api/history":
            self._json(load_history())
        elif path == "/api/stream":
            self._sse()
        elif path == "/api/colos":
            # 国家分组 + 各国家节点列表/IP 池大小，供前端下拉框
            by_cc = geoip.colo_list_by_cc()
            report = pools.pool_report()
            out = []
            for cc, colos in by_cc.items():
                out.append({
                    "cc": cc,
                    "cc_zh": geoip.country_zh(cc),
                    "count": len(colos),
                    "pool": sum(report.get(c["code"], 0) for c in colos),
                    "codes": [c["code"] for c in colos],
                    "items": [{"code": c["code"], "name": c["name"],
                               "pool": report.get(c["code"], 0)} for c in colos],
                })
            out.sort(key=lambda x: x["cc_zh"])
            self._json(out)
        elif path == "/api/export":
            # ?fmt=csv|json&source=latest|history&history_id=N
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            from urllib.parse import unquote
            params = {k: unquote(v) for k, v in params.items()}
            fmt = params.get("fmt", "csv")
            if fmt not in exports.FORMATS:
                self._json({"error": f"未知导出格式：{fmt}"}, 400)
                return
            source = params.get("source", "latest")
            result = None
            if source == "history":
                for e in load_history():
                    if str(e.get("id")) == params.get("history_id"):
                        result = e
                        break
            elif source == "latest":
                with manager.lock:
                    result = manager.last_result
            if not result or not result.get("results"):
                self._json({"error": "没有可导出的结果"}, 404)
                return
            inline = (self.headers.get("X-Inline") == "1")
            try:
                data = exports.export(result, fmt)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            body = data["content"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{data['ctype']}; charset=utf-8")
            self.send_header("Content-Disposition",
                             ("inline" if inline else "attachment") +
                             f'; filename="{data["filename"]}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/pools":
            self._json(pools.pools_detail())
        elif path == "/api/data-status":
            # 数据目录 / 池统计 概要，供前端信息栏
            d = geoip.DATA_DIR
            rep = pools.pool_report()
            st = {
                "version": __version__,
                "data_dir": str(d),
                "cf_cache": d.joinpath("cf_ips.json").stat().st_size if d.joinpath("cf_ips.json").exists() else 0,
                "cf_cidrs": len(ipdata.fetch_cf_ips().get("v4", [])),
                "pool_dc": len(rep),
                "pool_ips": sum(rep.values()),
                "pool_expired": pools.expired(),
                "colo_count": geoip.colo_count(),
                "running": manager.running,
                "python": ".".join(str(x) for x in sys.version_info[:3]),
            }
            self._json(st)
        else:
            self._json({"error": "not found"}, 404)

    # ── POST ──

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body_json()
        if path == "/api/scan":
            ok, err = manager.start(body)
            self._json({"error": err} if not ok else {"started": True})
        elif path == "/api/cancel":
            manager.cancel()
            self._json({"cancelled": True})
        elif path == "/api/history":
            act = body.get("action")
            if act == "delete":
                h = [x for x in load_history() if x.get("id") != body.get("id")]
                save_history(h)
                self._json({"ok": True})
            elif act == "clear":
                save_history([])
                self._json({"ok": True})
            else:
                self._json({"error": "bad action"}, 400)
        elif path == "/api/pools":
            act = body.get("action")
            if act == "clear":
                n = pools.clear_pool(body.get("code", ""))
                self._json({"ok": True, "removed": n})
            elif act == "clear_all":
                n = pools.clear_all()
                self._json({"ok": True, "removed": n})
            elif act == "add":
                # 手动补充 IP 入池：
                #   1) 校验 IP 是否在 CF IPv4 段内（TYOYO1/CF-ASN 全量段；不在 → 拒绝）
                #   2) 并发拨号读 cf-meta-colo 得到实际服务节点
                #   3) 按实际 colo 归池（或匹配 body 指定的 code）
                code = (body.get("code") or "").strip().upper()
                ips = [x.strip() for x in str(body.get("ips", "")).replace(",", "\n").splitlines() if x.strip()]
                if not ips:
                    self._json({"error": "缺少 ips"}, 400)
                    return
                res = pools.probe_and_add(ips, code, use_tls=True, workers=12)
                self._json({"ok": True, **res})
            else:
                self._json({"error": "bad action"}, 400)
        else:
            self._json({"error": "not found"}, 404)

    # ── SSE ──

    def _sse(self):
        with manager.lock:
            sc = manager.scanner
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        if sc is None:
            self.wfile.write(b'data: {"type":"none"}\n\n')
            self.wfile.flush()
            return

        q = sc.subscribe()
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except Exception:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                rep = pools.pool_report()
                state = {
                    "type": "state",
                    "running": item.get("running"),
                    "stage": item.get("stage"),
                    "pct": item.get("pct"),
                    "detail": item.get("detail"),
                    "elapsed": item.get("elapsed"),
                    "logs": item.get("logs", []),
                    # 池统计随状态流实时下发（否则前端 30s 定时刷新会让池数"卡住"，
                    # 后台填充入池时界面上看不出来）
                    "pool_dc": len(rep),
                    "pool_ips": sum(rep.values()),
                }
                self.wfile.write(b"data: " + json.dumps(state, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                if item.get("stage") in ("done", "error") and not item.get("running"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sc.unsubscribe(q)


# ── 启动 ──

def find_free_port(preferred: int | None = None, host: str = "127.0.0.1") -> int:
    """分配监听端口：preferred 非 0 且可用则用之，否则自动选空闲端口。
    探测时绑定真实监听地址（0.0.0.0 的可用性可能不同于 127.0.0.1）。"""
    import socket
    if preferred:
        try:
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, preferred))
                return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]


def run_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), FastCFHandler)
    server.daemon_threads = True
    return server


def start(host: str, port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """启动监听并返回 (server, 实际端口)。port=0 自动分配；监听失败抛 OSError。"""
    port = find_free_port(port or None, host=host)
    return run_server(host, port), port
