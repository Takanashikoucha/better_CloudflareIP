# -*- coding: utf-8 -*-
"""ICMP ping（系统 `ping` 命令，不手写 raw socket）。

- ping(ip, times=4, timeout=2)：4 包精确测量，返回 (avg_ms, loss)
- ping_probe(ip, timeout=2)：1 包快速探测，返回 (reachable, avg_ms)
中文/英文输出均解析 `rtt min/avg/max/mdev` 与 `N% packet loss` 行。
"""
import re
import subprocess

from . import config

# 预编译正则（中文/英文 ping 输出同形）
_RE_LOSS = re.compile(r"(\d+(?:\.\d+)?)%\s*packet\s*loss")
_RE_RTT = re.compile(r"=\s*[\d.]+/([\d.]+)/[\d.]+")


def _run_ping(args: list, timeout: float) -> str:
    try:
        out = subprocess.run(["ping"] + args, capture_output=True, text=True, timeout=timeout)
        return out.stdout or ""
    except Exception:
        return ""


def _parse(out: str) -> tuple:
    """解析 ping 输出 → (avg_ms, loss)。不可达返回 (0, 1.0)。"""
    m = _RE_LOSS.search(out)
    loss = 1.0
    if m:
        loss = float(m.group(1)) / 100.0
    r = _RE_RTT.search(out)
    avg_ms = float(r.group(1)) if r else 0.0
    if avg_ms > 0:
        avg_ms = max(1, round(avg_ms))  # 亚毫秒时延向上取整到 1ms
    else:
        avg_ms = 0
    if loss >= 1.0:
        return 0, 1.0
    return avg_ms, loss


def ping(ip: str, times: int = config.PING_TIMES, timeout: float = config.PING_TIMEOUT) -> tuple:
    """ICMP ping 精确测量。返回 (avg_ms, loss)；不可达返回 (0, 1.0)。

    注意：Linux iputils `ping -W` 单位为**秒**（<1 会静默变 0），故强制 ≥1。
    """
    wsec = max(1, int(timeout))
    out = _run_ping(["-c", str(times), "-W", str(wsec), ip], times * timeout + 10)
    return _parse(out)


def ping_probe(ip: str, timeout: float = config.PING_TIMEOUT) -> tuple:
    """ICMP 1 包探测（快速淘汰不可达 IP）。返回 (reachable, avg_ms)。"""
    wsec = max(1, int(timeout))
    out = _run_ping(["-c", "1", "-W", str(wsec), ip], timeout + 10)
    avg_ms, loss = _parse(out)
    if loss >= 1.0:
        return False, 0
    return True, avg_ms
