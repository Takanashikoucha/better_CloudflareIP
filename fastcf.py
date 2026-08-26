#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastCF — Cloudflare IP 优选测速工具（Linux · 零第三方依赖 · 直连）

用法：
    # Web 模式（默认）
    python3 fastcf.py                 # 启动并自动打开浏览器
    python3 fastcf.py --port 8080     # 指定端口
    python3 fastcf.py --no-browser    # 不自动打开浏览器
    python3 fastcf.py --data-dir /x   # 指定数据缓存目录

    # CLI 模式（参考 XIU2/CloudflareSpeedTest，--cli 之后的参数由 CLI 解析）
    python3 fastcf.py --cli
    python3 fastcf.py --cli -n 200 -t 10 -dn 20 -o result.csv
"""
import os
import sys
import time

# ── 直连保障：清除所有代理环境变量（含大小写），确保测速流量不经过任何代理 ──
for _k in list(os.environ.keys()):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"):
        del os.environ[_k]
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"


def main():
    argv = sys.argv[1:]

    # ── CLI 模式：--cli 之后的参数交给 cli.build_parser 解析 ──
    if "--cli" in argv:
        from fastcf import cli
        raise SystemExit(cli.main([a for a in argv if a != "--cli"]))

    # ── Web 模式（默认）──
    import argparse
    import webbrowser
    from fastcf import __version__, server, geoip

    ap = argparse.ArgumentParser(description="FastCF — Cloudflare IP 优选测速（Linux）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=0, help="监听端口（默认自动分配）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--data-dir", help="数据缓存目录（默认 ~/.fastcf）")
    ap.add_argument("--cli", action="store_true", help="进入 CLI 模式（参数在 --cli 之后）")
    ap.add_argument("--version", action="version", version=f"FastCF {__version__}")
    args = ap.parse_args()

    if args.data_dir:
        os.environ["FASTCF_HOME"] = os.path.abspath(args.data_dir)

    port = server.find_free_port(args.port or None)
    srv = server.run_server(args.host, port)
    url = f"http://{args.host}:{port}"

    # 后台预热地理库（下载 xdb + 建内存索引），完成后顺带预热就近 DC 的 IP 池
    from fastcf import pools

    def _preheat_pools():
        try:
            # 只预热 US/JP/HK（CN 出口质量最好的三个地区）
            pools.preheat_nearby(["US", "JP", "HK"], use_v6=False, use_tls=True,
                                 log=lambda m: print(f"[{time.strftime('%H:%M:%S')}][pools] {m}", flush=True))
        except Exception as e:
            print(f"[pools] 预热失败：{e}", flush=True)

    geoip.preload(on_done=_preheat_pools)

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
        srv.shutdown()


if __name__ == "__main__":
    main()
