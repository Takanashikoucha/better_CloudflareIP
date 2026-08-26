# -*- coding: utf-8 -*-
"""结果导出模块：CSV 表格（与 CFST 的 result.csv 风格对齐）+ 完整 JSON。"""
import csv as _csv
import io
import json


def _rows(result: dict) -> list:
    return result.get("results") or []


def to_csv(result: dict) -> str:
    """CSV 导出，表头与 CFST result.csv 风格对齐。"""
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["IP 地址", "平均延迟(ms)", "丢包率(%)", "峰值速度(Mbps)", "节点码", "节点中文名",
                "实际位置", "协议", "CF-RAY"])
    for r in _rows(result):
        loss = r.get("loss")
        w.writerow([
            r["ip"],
            r.get("latency", r.get("ping", 0)),
            ("" if loss is None else f"{round(loss * 100)}"),
            r.get("mbps", 0),
            r.get("dc") or "N/A",
            r.get("dc_zh") or "",
            r.get("location") or "",
            "TLS:443" if r.get("tls", True) else "HTTP:80",
            r.get("cfRay") or "",
        ])
    return buf.getvalue()


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


FORMATS = {
    "csv": ("CSV 表格", to_csv, ".csv", "text/csv"),
    "json": ("JSON 完整结果", to_json, ".json", "application/json"),
}


def export(result: dict, fmt: str) -> dict:
    """返回 {content, filename, ctype, label}；未知格式抛 ValueError。"""
    if fmt not in FORMATS:
        raise ValueError(f"未知导出格式：{fmt}")
    label, fn, ext, ctype = FORMATS[fmt]
    content = fn(result)
    return {
        "content": content,
        "label": label,
        "filename": f"fastcf_result{ext}",
        "ctype": ctype,
    }


def available() -> list:
    return [{"id": k, "label": v[0]} for k, v in FORMATS.items()]
