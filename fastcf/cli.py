# -*- coding: utf-8 -*-
"""CLI 模式（参考 XIU2/CloudflareSpeedTest 的命令行体验）。

fastcf.py --cli 进入终端测速：
  - 终端实时日志
  - 结束时按延迟打印结果表（对齐 CFST 的列格式）
  - 可选写入 CSV 结果文件（-o）
"""
import argparse
import sys
import time

from . import __version__, exports, geoip
from .scanner import Scanner


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fastcf.py",
        description="FastCF — Cloudflare IP 优选测速（CLI 模式）",
        epilog="""参数说明（参考 XIU2/CloudflareSpeedTest 的命名习惯）：
  -n N        候选池大小；每次扫描从 IP 池取 N 个测速（默认 100，最大 300）
  -t S        测速时长（秒）；单个 IP 下载测速时间（默认 8）
  -mb N       测速流量（MB）；下载流量上限（默认 50）
  -dn N       下载测速数量；ping 预筛选后取延迟最低的 N 个进入测速（默认 10，最大 30）
  -p N        显示结果数量（默认 5，0 时不显示直接退出）
  -tl N       延迟上限；只输出延迟低于 N ms 的结果（默认不过滤）
  -o FILE     写入结果 CSV 文件；留空则不写（默认 result.csv）
  -v6         使用 IPv6（默认 IPv4）
  -no-tls     使用 HTTP:80 测速（默认 TLS:443）
  --colo X    指定 DC 节点（三字码）或 RANDOM 全局随机（默认按国家组就近）
  --countries A,B,C  就近国家组（逗号分隔 ISO 码，默认 CN,HK,MO,TW,JP,KR,SG,MY）
  -d          调试输出""")
    ap.add_argument("-n", "--pool", type=int, default=100, help="候选池大小（默认 100）")
    ap.add_argument("-t", "--secs", type=int, default=8, help="测速时长秒（默认 8）")
    ap.add_argument("-mb", "--mb", type=int, default=50, help="测速流量 MB（默认 50）")
    ap.add_argument("-dn", "--download-num", type=int, default=10,
                    help="下载测速数量（默认 10）")
    ap.add_argument("-p", "--print-num", type=int, default=5, help="显示结果数量（默认 5，0=不显示）")
    ap.add_argument("-tl", "--max-latency", type=int, default=0, help="延迟上限 ms（默认不过滤）")
    ap.add_argument("-o", "--output", default="result.csv", help="结果 CSV 文件（空字符串=不写）")
    ap.add_argument("-v6", action="store_true", help="使用 IPv6")
    ap.add_argument("-no-tls", action="store_true", help="使用 HTTP:80（默认 TLS:443）")
    ap.add_argument("--colo", default="", help="指定 DC 三字码 / RANDOM")
    ap.add_argument("--countries", default="CN,HK,MO,TW,JP,KR,SG,MY", help="就近国家组")
    ap.add_argument("-d", "--debug", action="store_true", help="调试输出")
    ap.add_argument("--version", action="version", version=f"FastCF {__version__}")
    return ap


def run(params: dict, args) -> int:
    from . import pools  # 延迟导入避免循环

    # 先做参数钳位，保证终端摘要与扫描实际使用的值一致
    pool_n = max(20, min(300, args.pool))
    secs = max(3, min(60, args.secs))
    mb = max(10, min(1000, args.mb))
    dn = max(3, min(30, args.download_num))
    count = max(1, args.print_num or 1)
    sample = max(50, min(3000, pool_n * 3))

    # 调整全局池参数（-n 影响单次扫描取用的数量）
    pools.TEST_SIZE = pool_n

    scan = {
        "ipVer": "v6" if args.v6 else "v4",
        "tls": not args.no_tls,
        "count": count,
        "colo": args.colo,
        "countries": [c.strip().upper() for c in args.countries.split(",") if c.strip()],
        "sample": sample,
        "speedSecs": secs,
        "speedMB": mb,
        "top_rtt": dn,
    }

    def cb(msg, level="info"):
        mark = {"ok": "✔", "warn": "⚠", "error": "✘"}.get(level, "·")
        print(f"  {mark} {msg}", flush=True)

    print()
    print(f"# FastCF v{__version__} — Cloudflare IP 优选（CLI 模式）")
    print(f"  IPv{'6' if args.v6 else '4'} | TLS={'开' if not args.no_tls else '关'} | "
          f"候选={pool_n} | 测速 {dn} 个 × {secs}s/{mb}MB | "
          f"返回 Top{count}")
    print()

    t0 = time.time()
    sc = Scanner(scan)
    sc.log = cb
    try:
        sc.run()
    except KeyboardInterrupt:
        sc.cancel()
    elapsed = int(time.time() - t0)

    res = sc.result_payload or {}
    if res.get("error"):
        print(f"\n[错误] {res['error']}", file=sys.stderr)
        return 1
    results = res.get("results", [])
    if not results:
        print("\n[信息] 没有可用的测速结果。", file=sys.stderr)
        return 1

    # 延迟上限过滤
    if args.max_latency > 0:
        results = [r for r in results if (r.get("latency") or 10**9) <= args.max_latency]

    # CSV 输出（对齐 CFST 的 result.csv）
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(exports.to_csv({"results": results, "ipVer": scan["ipVer"]}))
            print(f"\n[信息] 完整测速结果已写入 {args.output}，可用表格软件查看。")
        except OSError as e:
            print(f"\n[警告] 写入 {args.output} 失败：{e}", file=sys.stderr)

    # 终端表格（对齐 CFST 的 Print 列格式）
    if args.print_num > 0 and results:
        show = results[: args.print_num]
        has6 = any(len(r["ip"]) > 15 for r in show)
        if has6:
            head = "%-40s%-8s%-14s%-8s%-6s\n"
            rowf = "%-42s%-8s%-16s%-10s%-8s\n"
        else:
            head = "%-18s%-8s%-14s%-8s%-6s\n"
            rowf = "%-20s%-8s%-16s%-10s%-8s\n"
        print()
        print(head % ("IP 地址", "平均延迟", "下载速度", "节点", "地区码"))
        for r in show:
            print(rowf % (
                r["ip"],
                f"{r.get('latency', 0)} ms",
                f"{r.get('mbps', 0)} Mbps",
                r.get("dc_zh") or r.get("dc") or "N/A",
                r.get("dc") or "N/A",
            ))
    print(f"\n[完成] 用时 {elapsed}s，共 {len(results)} 个结果。")
    return 0


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    # 预热 colo 参考数据（静态快照兜底，不会阻塞）
    geoip.ensure_colo_data()
    return run({}, args)


if __name__ == "__main__":
    raise SystemExit(main())
