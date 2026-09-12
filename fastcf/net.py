# -*- coding: utf-8 -*-
"""网络工具：绕过代理直连下载（带重试退避）+ 原子写盘。"""
import time
import urllib.request


def direct_download(url: str, timeout: int = 30, retries: int = 3) -> str:
    """绕过代理直连下载文本。偶发中断（TLS EOF / 读超时）自动重试，退避 1s/2s。

    注意：urllib 的 timeout 只约束**已建立连接后的 socket 操作**，
    不约束 connect 阶段（慢网络下 TCP/TLS 握手可能远超 timeout）。
    因此这里用**总时间预算**兜底：整个下载（含全部重试）超过 budget 秒即放弃，
    防止慢握手把调用方（含扫描线程）拖死。
    """
    budget = timeout * retries + 5
    t0 = time.monotonic()
    last_err = None
    for attempt in range(retries):
        if time.monotonic() - t0 > budget:
            break  # 预算耗尽，不再尝试
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FastCF/4.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last_err = e
            if time.monotonic() - t0 > budget:
                break
            if attempt < retries - 1:
                time.sleep(1 + attempt)
    raise last_err if last_err else RuntimeError(f"下载超时（{url}）")


def download(url: str, timeout: int = 60, min_size: int = 0) -> str:
    """直连下载文本；文件过小视为失败。"""
    text = direct_download(url, timeout=timeout)
    if min_size and len(text.encode("utf-8", "ignore")) < min_size:
        raise RuntimeError(f"下载内容过小（{len(text)} 字符），视为失败")
    return text


def atomic_write(path, data: str):
    """原子落盘（临时文件 + rename），避免中途失败留下截断文件。"""
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    import os
    os.replace(tmp, path)
