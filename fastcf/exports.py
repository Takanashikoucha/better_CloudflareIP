# -*- coding: utf-8 -*-
"""结果导出：CSV 表格（与 CFST 的 result.csv 风格对齐）+ 完整 JSON。"""
import csv as _csv
import io
import json

FORMATS = ("csv", "json")


def to_csv(result: dict) -> str:
    """CSV 导出，表头与 CFST result.csv 风格对齐。"""
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["IP 地址", "平均延迟(ms)", "丢包率(%)", "峰值速度(Mbps)", "节点码",
                "节点中文名", "实际位置", "协议", "CF-RAY"])
    for r in result.get("results") or []:
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


def export(result: dict, fmt: str) -> dict:
    """返回 {content, filename, ctype, label}；未知格式抛 ValueError。"""
    if fmt not in FORMATS:
        raise ValueError(f"未知导出格式：{fmt}")
    if fmt == "csv":
        return {"content": to_csv(result), "filename": "fastcf_result.csv",
                "ctype": "text/csv", "label": "CSV 表格"}
    return {"content": to_json(result), "filename": "fastcf_result.json",
            "ctype": "application/json", "label": "JSON 完整结果"}
