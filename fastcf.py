#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastCF — Cloudflare IP 优选测速工具（深色仪表盘 · FastAPI）

固定口径：IPv4 · 443/TLS · 结果 5 个。
模式：指定 DC / 全局随机；无后台扫描线程，IP 池靠手动添加 + 扫描副产品。

用法：
    python3 fastcf.py                 # 启动并自动打开浏览器
    python3 fastcf.py --port 8080     # 指定端口
    python3 fastcf.py --no-browser    # 不自动打开浏览器
    python3 fastcf.py --data-dir /x   # 指定数据缓存目录
"""
import os
import sys


def _ensure_vendor():
    """把项目内 .vendor（fastapi/uvicorn）加入 sys.path（系统 site-packages 只读时的兜底）。"""
    v = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vendor")
    if os.path.isdir(v) and v not in sys.path:
        sys.path.insert(0, v)


def main():
    # ── 直连保障：清除所有代理环境变量（含大小写），确保测速流量不经过任何代理 ──
    for _k in list(os.environ.keys()):
        if _k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"):
            del os.environ[_k]
    os.environ["no_proxy"] = "*"
    os.environ["NO_PROXY"] = "*"

    _ensure_vendor()

    import argparse
    import webbrowser

    import uvicorn
    from fastcf import __version__, server

    ap = argparse.ArgumentParser(description="FastCF — Cloudflare IP 优选测速")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=0, help="监听端口（默认自动分配）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--data-dir", help="数据缓存目录（默认 ~/.fastcf）")
    ap.add_argument("--version", action="version", version=f"FastCF {__version__}")
    args = ap.parse_args()

    if args.data_dir:
        os.environ["FASTCF_HOME"] = os.path.abspath(args.data_dir)

    # 端口分配：preferred 非 0 且可用则用之，否则自动选空闲端口
    import socket
    port = args.port
    if port:
        try:
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((args.host, port))
        except OSError:
            port = 0
    if not port:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((args.host, 0))
            port = s.getsockname()[1]

    url = f"http://{args.host}:{port}"
    print("=" * 56)
    print(f"  FastCF v{__version__} — Cloudflare IP 优选")
    print(f"  数据目录：{os.environ.get('FASTCF_HOME', os.path.expanduser('~/.fastcf'))}")
    print(f"  打开浏览器访问：{url}")
    print("  按 Ctrl+C 退出")
    print("=" * 56)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        uvicorn.run(server.app, host=args.host, port=port,
                    log_level="warning", lifespan="on")
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
