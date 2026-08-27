#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastCF 离线单元测试（零第三方依赖，不触网）。

运行：python3 tests/test_units.py
"""
import ipaddress
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["FASTCF_HOME"] = tempfile.mkdtemp(prefix="fastcf-test-")

from fastcf import exports, geoip, ipdata, pools, scanner  # noqa: E402


def test_v4_prefixes():
    # /8 → 65536 个 /24
    pl = list(ipdata.v4_prefixes(ipaddress.ip_network("1.0.0.0/8"), 24))
    assert len(pl) == 65536, len(pl)
    assert str(pl[0]) == "1.0.0.0/24", pl[0]
    assert str(pl[-1]) == "1.255.255.0/24", pl[-1]
    assert str(pl[16000]) == "1.62.128.0/24", pl[16000]
    # /16 → 256 个 /24
    pl16 = list(ipdata.v4_prefixes(ipaddress.ip_network("1.2.0.0/16"), 24))
    assert len(pl16) == 256 and str(pl16[0]) == "1.2.0.0/24" and str(pl16[-1]) == "1.2.255.0/24"
    # 版本校验
    try:
        list(ipdata.v4_prefixes(ipaddress.ip_network("2606:4700::/32")))
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass


def test_pools():
    pools.clear_all()
    pools.add("LAX", ["1.1.1.1", "1.1.1.2"])
    pools.add("LAX", ["1.1.1.2", "1.1.1.3"])  # 去重
    assert pools.get("LAX") == ["1.1.1.1", "1.1.1.2", "1.1.1.3"]
    pools.remove("LAX", ["1.1.1.2"])
    assert pools.get("LAX") == ["1.1.1.1", "1.1.1.3"]
    assert pools.size("lax") == 2  # 大小写不敏感
    assert pools.pool_report() == {"LAX": 2}
    assert pools.pools_detail()[0]["code"] == "LAX"
    # 持久化往返
    data = json.loads(pools.POOL_FILE.read_text())
    assert data["pools"]["LAX"]["ips"] == ["1.1.1.1", "1.1.1.3"]
    # 截断：超过 max_size 保留最新
    pools.clear_all()
    big = [f"9.9.9.{i}" for i in range(pools.POOL_SIZE + 10)]
    pools.add("SFO", big)
    assert pools.size("SFO") == pools.POOL_SIZE
    assert pools.get("SFO")[-1] == big[-1]
    pools.clear_all()
    assert pools.get("LAX") == []


def test_pool_cap_no_bloat():
    # 池上限 POOL_SIZE(50) 防大小爆炸：
    # ① 单批超量只保留最新 50（保留尾部，丢弃头部）
    big = [f"9.9.9.{i}" for i in range(pools.POOL_SIZE + 30)]
    pools.add("SFO", big)
    assert pools.size("SFO") == pools.POOL_SIZE
    assert pools.get("SFO") == big[-pools.POOL_SIZE:]
    # ② 多批持续追加仍恒为 50
    for _ in range(5):
        pools.add("SFO", [f"9.9.8.{i}" for i in range(pools.POOL_SIZE)])
    assert pools.size("SFO") == pools.POOL_SIZE
    # ③ 持久化文件里也只有 50 个（不会膨胀落盘）
    data = json.loads(pools.POOL_FILE.read_text())
    assert len(data["pools"]["SFO"]["ips"]) == pools.POOL_SIZE
    pools.clear_all()


def test_expired_single_dc():
    # 单 DC 事件性过期判断：整体不过期但某 DC 过期 → expired(code) 为 True
    pools.clear_all()
    pools.add("LAX", ["1.1.1.1"])
    pools.add("SFO", ["2.2.2.2"])
    import fastcf.pools as _p
    with _p._lock:
        _p._pool_ts["LAX"] = time.time() - _p.TTL - 1
    try:
        assert pools.expired("LAX")
        assert not pools.expired("SFO")
        # touch 刷新后不再过期
        pools.touch("LAX")
        assert not pools.expired("LAX")
    finally:
        pools.clear_all()


def test_expired_global():
    pools.clear_all()
    pools.add("LAX", ["1.1.1.1"])
    assert not pools.expired()
    # 模拟整体过期
    import fastcf.pools as _p
    old = _p._pools_ts
    _p._pools_ts = time.time() - _p.TTL - 1
    try:
        assert pools.expired()
    finally:
        _p._pools_ts = old
        pools.clear_all()


def _sample_result():
    return {
        "count": 1, "elapsed": 5, "ipVer": "v4", "tls": True,
        "mode": "RANDOM", "colo": None, "randomCount": 150, "minSpeed": 0,
        "results": [{
            "ip": "1.2.3.4", "ping": 20, "latency": 20, "loss": 0.0,
            "mbps": 123, "port": 443, "dc": "LAX", "dc_zh": "美国·洛杉矶",
            "cfRay": "abc-LAX-9e21", "location": "美国·洛杉矶", "tls": True,
        }],
    }


def test_exports():
    res = _sample_result()
    csv = exports.to_csv(res)
    assert csv.splitlines()[0].startswith("IP 地址")
    assert "1.2.3.4" in csv and "TLS:443" in csv and "美国·洛杉矶" in csv
    js = json.loads(exports.to_json(res))
    assert js["results"][0]["ip"] == "1.2.3.4"
    out = exports.export(res, "csv")
    assert out["filename"].endswith(".csv") and out["ctype"] == "text/csv"
    try:
        exports.export(res, "nope")
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass


def test_geoip():
    assert geoip.colo_zh("LAX") and geoip.colo_country("LAX") == "US"
    assert geoip.colo_zh("") == ""
    assert geoip.country_zh("") == "未知"
    assert geoip.country_zh("CN") == "中国"
    assert geoip.country_zh("XX") == "XX"  # 未命中返回原码
    bycc = geoip.colo_list_by_cc()
    assert "US" in bycc
    assert all(c["code"] for c in bycc["US"])
    # 组内按 code 排序
    codes = [c["code"] for c in bycc["US"]]
    assert codes == sorted(codes)
    assert geoip.colo_count() > 300


def test_prefix_edge_cases():
    # 版本错误 → ValueError
    try:
        list(ipdata.v4_prefixes(ipaddress.ip_network("2606:4700::/32"), 24))
        assert False, "should raise"
    except ValueError:
        pass
    # plen <= prefixlen → 原样返回
    pl = list(ipdata.v4_prefixes(ipaddress.ip_network("1.2.3.0/24"), 24))
    assert len(pl) == 1 and str(pl[0]) == "1.2.3.0/24"
    # 采样函数签名存在
    assert callable(ipdata.sample_cf_ips)


def test_icmp_ping_unreachable():
    # TEST-NET 地址必然不可达：返回 (0, 1.0)，不抛异常
    avg, loss = scanner.icmp_ping("192.0.2.1", times=1, timeout=1)
    assert avg == 0 and loss == 1.0


def test_icmp_ping_loopback():
    # 本机回环必然可达：loss=0，avg>0
    avg, loss = scanner.icmp_ping("127.0.0.1", times=2, timeout=2)
    assert loss == 0.0 and avg > 0, (avg, loss)


def test_scanner_constants():
    # 固定口径：结果 5 个、ping 4 包、并发 200
    assert scanner.RESULT_COUNT == 5
    assert scanner.PING_TIMES == 4
    assert scanner.PING_WORKERS == 200
    # 不再有 TCP+TLS 冒充 ping 的旧口径
    assert not hasattr(scanner, "_tcp_tls_ms")


def test_imports():
    import fastcf.server
    assert fastcf.scanner.RESULT_COUNT == 5
    # 后台填充线程已移除
    import importlib
    try:
        importlib.import_module("fastcf.filler")
        raise AssertionError("fastcf.filler 应当已被删除")
    except ImportError:
        pass


def test_sample_cf_subnets_small_prefix():
    # 回归：CF 官方段中存在比目标更小的子网（如 /28），必须保留
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cf_ips.json")
        with open(p, "w") as f:
            json.dump({
                "ts": time.time(),
                "v4": ["192.0.2.0/24", "198.51.100.0/28"],   # /28 更小，必须保留
            }, f)
        p_old = ipdata.CF_IPS_CACHE
        ipdata.CF_IPS_CACHE = Path(p)
        try:
            ips = ipdata.sample_cf_ips(10, use_v6=False)
        finally:
            ipdata.CF_IPS_CACHE = p_old
        assert len(ips) > 0, "采样应能覆盖更小子网"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  ✔ {name}  ({(time.perf_counter() - t0) * 1000:.0f}ms)")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  ✘ {name}: {e}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failed else 'ALL OK'}（{len([k for k in globals() if k.startswith('test_')]) - failed} 通过 / {failed} 失败）")
    sys.exit(1 if failed else 0)
