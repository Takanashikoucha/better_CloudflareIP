# -*- coding: utf-8 -*-
"""结果导出模块：把扫描结果转成多种常用格式。

格式列表：
  iplist  — IP 列表（每行一个）
  ipport  — IP:端口（每行一个）
  csv     — CSV 表格（与 CFST 的 result.csv 对齐：IP/延迟/速度/节点…）
  json    — 完整 JSON 结果
  mihomo  — mihomo / Clash 直连节点
  singbox — sing-box direct 出站 + ip_cidr 规则
"""
import csv as _csv
import io
import json


def _rows(result: dict) -> list:
    return result.get("results") or []


def to_iplist(result: dict) -> str:
    return "\n".join(r["ip"] for r in _rows(result))


def to_ipport(result: dict) -> str:
    return "\n".join(f"{r['ip']}:{r.get('port', 443)}" for r in _rows(result))


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
            "TLS:443" if r.get("port", 443) == 443 else "HTTP:80",
            r.get("cfRay") or "",
        ])
    return buf.getvalue()


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def to_mihomo(result: dict) -> str:
    """mihomo / Clash 直连节点。"""
    lines = []
    for r in _rows(result):
        name = f"{r.get('dc_zh') or r.get('dc') or r['ip']} · {r.get('mbps', 0)}Mbps"
        lines.extend([
            f"- name: {name}",
            "  type: direct",
            f"  server: {r['ip']}",
            f"  port: {r.get('port', 443)}",
            "  udp: true",
            "  skip-cert-verify: true",
            f"  tls: {str(r.get('port', 443) == 443).lower()}",
        ])
    return "\n".join(lines)


def to_singbox(result: dict) -> str:
    """sing-box：direct 出站 + ip_cidr 规则。"""
    ipver = result.get("ipVer", "v4")
    plen = 128 if ipver == "v6" else 32
    out = {
        "out": [
            {
                "type": "direct",
                "tag": f"cf-{r.get('dc') or r['ip']}",
            }
            for r in _rows(result)
        ],
        "rule": [
            {
                "type": "ip_cidr",
                "ip": f"{r['ip']}/{plen}",
                "out": f"cf-{r.get('dc') or r['ip']}",
            }
            for r in _rows(result)
        ],
    }
    return json.dumps(out, ensure_ascii=False, indent=2)


FORMATS = {
    "iplist": ("IP 列表（多行）", to_iplist, ".txt", "text/plain"),
    "ipport": ("IP:端口", to_ipport, ".txt", "text/plain"),
    "csv": ("CSV 表格", to_csv, ".csv", "text/csv"),
    "json": ("JSON 完整结果", to_json, ".json", "application/json"),
    "mihomo": ("mihomo / Clash 直连节点", to_mihomo, ".yml", "text/yaml"),
    "singbox": ("sing-box direct 规则", to_singbox, ".json", "application/json"),
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
