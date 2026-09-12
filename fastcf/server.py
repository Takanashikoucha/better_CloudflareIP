# -*- coding: utf-8 -*-
"""FastAPI 服务：静态 Web UI + JSON API + SSE 实时日志流。"""
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__, colos, exports, history, pool
from .appstate import AppState
from .config import ScanParams

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="FastCF", version=__version__)
state = AppState()  # 进程级单例：所有端点共享同一状态源


# ── 静态资源（启动时读入内存，内容小；版本号单一来源替换）──

_STATIC: dict = {}


def _load_static():
    _STATIC["/"] = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace(
        "v__VERSION__", f"v{__version__}")
    _STATIC["/app.js"] = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    _STATIC["/style.css"] = (WEB_DIR / "style.css").read_text(encoding="utf-8")


@app.on_event("startup")
def _startup():
    _load_static()
    # 后台预热：colo 参考数据 + 双源 IP 缓存（均带 TTL，失败沿用旧缓存/快照）
    # 异常兜底：预热失败不影响服务启动（TTL 机制会在下次访问时重试）
    from . import sources
    def _safe(fn):
        try:
            fn()
        except Exception as e:
            print(f"[startup] 预热失败（{fn.__name__}）：{e}", flush=True)
    threading.Thread(target=lambda: _safe(colos.colos.refresh), daemon=True).start()
    threading.Thread(target=lambda: _safe(sources.fetch_cf_ips), daemon=True).start()
    threading.Thread(target=lambda: _safe(sources.fetch_external_ips), daemon=True).start()


# ── 静态页面 ──

@app.get("/")
def index():
    return Response(_STATIC["/"], media_type="text/html; charset=utf-8")


@app.get("/app.js")
def app_js():
    return Response(_STATIC["/app.js"], media_type="application/javascript; charset=utf-8")


@app.get("/style.css")
def style_css():
    return Response(_STATIC["/style.css"], media_type="text/css; charset=utf-8")


# ── 状态 / 历史 / 数据源 ──

@app.get("/api/status")
def api_status():
    return state.status()


@app.get("/api/history")
def api_history():
    return history.load()


@app.post("/api/history")
async def api_history_op(request: Request):
    body = await request.json()
    act = body.get("action")
    if act == "delete":
        return {"ok": history.delete(body.get("id"))}
    if act == "clear":
        history.clear()
        return {"ok": True}
    return JSONResponse({"error": "bad action"}, status_code=400)


@app.get("/api/colos")
def api_colos():
    # 国家分组（中国系置顶）+ 各节点池大小，供前端下拉框
    report = pool.pool_report()
    groups = []
    for g in colos.colos.groups():
        groups.append({
            "cc": g["cc"],
            "cc_zh": g["cc_zh"],
            "count": len(g["items"]),
            "pool": sum(report.get(i["code"], 0) for i in g["items"]),
            "items": [{**i, "pool": report.get(i["code"], 0)} for i in g["items"]],
        })
    return groups


@app.get("/api/pools")
def api_pools():
    return pool.pools_detail()


@app.post("/api/pools")
async def api_pools_op(request: Request):
    body = await request.json()
    act = body.get("action")
    if act == "clear":
        return {"ok": True, "removed": pool.clear_pool(body.get("code", ""))}
    if act == "clear_all":
        return {"ok": True, "removed": pool.clear_all()}
    if act == "remove_ip":
        code = (body.get("code") or "").strip().upper()
        ip = (body.get("ip") or "").strip()
        if not code or not ip:
            return JSONResponse({"error": "缺少 code 或 ip"}, status_code=400)
        ok = pool.remove_ip(code, ip)
        return {"ok": ok, "removed": ok}
    if act == "add":
        # 手动补充 IP 入池：已知来源校验 + 并发拨号读 cf-meta-colo 按实际节点归池
        code = (body.get("code") or "").strip().upper()
        ips = [x.strip() for x in str(body.get("ips", "")).replace(",", "\n").splitlines() if x.strip()]
        if not ips:
            return JSONResponse({"error": "缺少 ips"}, status_code=400)
        res = pool.probe_and_add(ips, code, workers=12)
        return {"ok": True, **res}
    return JSONResponse({"error": "bad action"}, status_code=400)


@app.get("/api/data-status")
def api_data_status():
    return state.data_status()


# ── 扫描（入口参数校验：非法参数 422，不启动扫描线程）──

@app.post("/api/scan")
async def api_scan(request: Request):
    body = await request.json()
    try:
        params = ScanParams(**body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    err = params.validate()
    if err:
        return JSONResponse({"error": err}, status_code=422)
    ok, err = state.start(params.model_dump())
    return JSONResponse({"error": err}, status_code=409) if not ok else {"started": True}


@app.post("/api/cancel")
def api_cancel():
    state.cancel()
    return {"cancelled": True}


# ── 导出 ──

@app.get("/api/export")
def api_export(fmt: str = "csv", source: str = "latest", history_id: int | None = None,
               x_inline: str = "0"):
    if fmt not in exports.FORMATS:
        return JSONResponse({"error": f"未知导出格式：{fmt}"}, status_code=400)
    result = None
    if source == "history" and history_id is not None:
        for e in history.load():
            if e.get("id") == history_id:
                result = e
                break
    elif source == "latest":
        result = state.last_result
    if not result or not result.get("results"):
        return JSONResponse({"error": "没有可导出的结果"}, status_code=404)
    try:
        data = exports.export(result, fmt)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return Response(
        content=data["content"].encode("utf-8"),
        media_type=f"{data['ctype']}; charset=utf-8",
        headers={"Content-Disposition":
                 ("inline" if x_inline == "1" else "attachment")
                 + f'; filename="{data["filename"]}"'},
    )


# ── SSE 实时日志流 ──

@app.get("/api/stream")
def api_stream():
    sc = state.scanner
    # 竞态：扫描已结束（done）→ 直接下发最终态并关闭，避免空等
    if sc is not None and sc.done.is_set():
        async def gen():
            final = sc.last_state or {"running": False, "stage": "done", "pct": 100}
            yield b"data: " + json.dumps(final, ensure_ascii=False).encode() + b"\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                headers={"Cache-Control": "no-store", "Connection": "close"})

    async def gen():
        if sc is None:
            yield b'data: {"type":"none"}\n\n'
            return
        q = sc.subscribe()
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except Exception:
                    # 超时：检查扫描是否已结束（兜底，防止 done 事件丢失导致前端挂起）
                    if sc.done.is_set():
                        break
                    yield b": ping\n\n"
                    continue
                if item is None:
                    break
                rep = pool.pool_report()
                s = {
                    "type": "state",
                    "running": item.get("running"),
                    "stage": item.get("stage"),
                    "pct": item.get("pct"),
                    "detail": item.get("detail"),
                    "elapsed": item.get("elapsed"),
                    # 日志增量推送：logDelta = 自上次推送以来新增；
                    # 迟到订阅者首帧 logs 为全量（logDelta 为空），前端据此重置本地日志
                    "logs": item.get("logs", []),
                    "logDelta": item.get("logDelta", []),
                    "logTotal": item.get("logTotal", 0),
                    # 池统计随状态流实时下发（否则前端定时刷新会让池数"卡住"）
                    "pool_dc": len(rep),
                    "pool_ips": sum(rep.values()),
                }
                yield b"data: " + json.dumps(s, ensure_ascii=False).encode() + b"\n\n"
                if item.get("stage") in ("done", "error") and not item.get("running"):
                    break
        finally:
            sc.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-store",
                                   "X-Accel-Buffering": "no"})
