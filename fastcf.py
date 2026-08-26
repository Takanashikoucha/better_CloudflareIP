#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastCF — Cloudflare IP 优选测速工具（Linux · 零第三方依赖 · 直连）

用法：
    python3 fastcf.py                 # 启动并自动打开浏览器
    python3 fastcf.py --port 8080     # 指定端口
    python3 fastcf.py --no-browser    # 不自动打开浏览器
    python3 fastcf.py --data-dir /x   # 指定数据缓存目录
"""
import os
import sys

# ── 直连保障：清除所有代理环境变量（含大小写），确保测速流量不经过任何代理 ──
for _k in list(os.environ.keys()):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"):
        del os.environ[_k]
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"


def main():
    import argparse
    import webbrowser
    from fastcf import __version__, server, geoip, filler

    ap = argparse.ArgumentParser(description="FastCF — Cloudflare IP 优选测速（Linux）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=0, help="监听端口（默认自动分配）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--data-dir", help="数据缓存目录（默认 ~/.fastcf）")
    ap.add_argument("--version", action="version", version=f"FastCF {__version__}")
    args = ap.parse_args()

    if args.data_dir:
        os.environ["FASTCF_HOME"] = os.path.abspath(args.data_dir)

    port = server.find_free_port(args.port or None, host=args.host)
    srv = server.run_server(args.host, port)
    url = f"http://{args.host}:{port}"

    # 后台预热：刷新 colo 参考数据 → 启动常驻 IP 池填充线程
    def _fill_log(msg):
        import time
        print(f"[{time.strftime('%H:%M:%S')}][filler] {msg}", flush=True)

    filler.start(log=_fill_log)
    geoip.preload()

    print("=" * 56)
    print(f"  FastCF v{__version__} — Cloudflare IP 优选")
    print(f"  数据目录：{geoip.DATA_DIR}")
    print(f"  打开浏览器访问：{url}")
    print("  按 Ctrl+C 退出")
    print("=" * 56)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        filler.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
