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
    bycc = {g["cc"]: g for g in geoip.colo_list_by_cc()}
    assert "US" in bycc
    # 组内按 code 排序
    codes = [i["code"] for i in bycc["US"]["items"]]
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


def test_is_in_cf_v4():
    # 段归属校验（显式传 cidrs，不触网）
    cidrs = ["192.0.2.0/24", "198.51.100.0/28"]
    assert ipdata.is_in_cf_v4("192.0.2.1", cidrs)
    assert ipdata.is_in_cf_v4("192.0.2.254", cidrs)
    assert ipdata.is_in_cf_v4("198.51.100.14", cidrs)
    assert not ipdata.is_in_cf_v4("198.51.100.16", cidrs)  # /28 边界外（.0–.15 在内）
    assert not ipdata.is_in_cf_v4("203.0.113.1", cidrs)     # 不在任何段
    # 非法 / 非 v4 输入 → False
    assert not ipdata.is_in_cf_v4("not-an-ip", cidrs)
    assert not ipdata.is_in_cf_v4("2606:4700::1", cidrs)
    assert not ipdata.is_in_cf_v4("192.0.2.1", [])          # 空段列表
    # 段内非法 CIDR 行被跳过、不抛异常
    assert ipdata.is_in_cf_v4("192.0.2.1", ["badcidr", "192.0.2.0/24"])


def test_first_ip_per_segment():
    # 段首 IP（池初始化用）：取每段首个可用主机，.0 网络地址跳过，/31、/32 跳过，
    # 重复首 IP 去重，非法行跳过（显式传 cidrs，不触网）
    out = ipdata.first_ip_per_segment([
        "192.0.2.0/24",        # → 192.0.2.1
        "198.51.100.0/28",     # → 198.51.100.1
        "203.0.113.0/31",      # /31 跳过（首地址是网络地址，不可靠）
        "203.0.113.5/32",      # 单主机 /32 → 203.0.113.5
        "192.0.2.128/25",      # → 192.0.2.129
        "badcidr",             # 非法行，跳过
        "198.51.100.0/28",     # 与上面重复段，首 IP 去重
    ])
    assert out == ["192.0.2.1", "198.51.100.1", "203.0.113.5", "192.0.2.129"], out
    assert ipdata.first_ip_per_segment([]) == []
    # 探测入口签名存在（实际拨号不测，离线）
    assert callable(ipdata.segment_first_ips_probe)


def test_parse_ext_lines():
    # 外部清单解析：仅保留 443 端口、合法 IPv4，去重，其余静默跳过
    text = (
        "1.1.1.1:443#US\n"        # 保留
        "2.2.2.2:8443#DE\n"       # 非 443 丢弃
        "3.3.3.3:2053#SG\n"       # 非 443 丢弃
        "4.4.4.4:443\n"           # 无标签，保留
        "4.4.4.4:443#US\n"        # 重复 IP 去重
        "999.1.1.1:443#XX\n"      # 非法 IPv4 丢弃
        "garbage line\n"          # 非法行丢弃
        "5.6.7.8:443#NL   \n"     # 尾随空白，保留
        "\n"
    )
    ips, kept, skipped = ipdata.parse_ext_lines(text)
    assert ips == ["1.1.1.1", "4.4.4.4", "5.6.7.8"], ips
    assert kept == 3 and skipped == 6, (kept, skipped)
    assert ipdata.parse_ext_lines("") == ([], 0, 0)
    # 非法 octet（>255）被 ipaddress 拒绝
    ips2, _, _ = ipdata.parse_ext_lines("300.0.0.1:443#US\n")
    assert ips2 == []


def test_sample_cf_ips_dual_source():
    # 双源合并采样：mock 两源缓存，验证 50/50 混合、去重、单边缺失退化
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p_cf = os.path.join(d, "cf_ips.json")
        p_ext = os.path.join(d, "ext_ips.json")
        # 官方段：/24 → 只能采到 192.0.2.x；外部清单：203.0.113.x
        with open(p_cf, "w") as f:
            json.dump({"ts": time.time(), "v4": ["192.0.2.0/24"], "source": "official"}, f)
        with open(p_ext, "w") as f:
            json.dump({"ts": time.time(),
                       "v4": ["203.0.113.1", "203.0.113.2", "203.0.113.3"],
                       "source": "ext"}, f)
        old_cf, old_ext = ipdata.CF_IPS_CACHE, ipdata.EXT_IPS_CACHE
        ipdata.CF_IPS_CACHE = Path(p_cf)
        ipdata.EXT_IPS_CACHE = Path(p_ext)
        try:
            ips = ipdata.sample_cf_ips(10)
            # 官方侧最多 254 个但 /24 分层每轮每块 3 个 → 10 个里官方约 5 个
            # 外部侧 3 个全取，其余名额由官方补
            assert len(ips) == 10, len(ips)
            assert len(set(ips)) == 10, "必须去重"
            ext_part = [i for i in ips if i.startswith("203.0.113.")]
            assert len(ext_part) == 3, f"外部 3 个应全部进入样本：{ext_part}"
            off_part = [i for i in ips if i.startswith("192.0.2.")]
            assert len(off_part) == 7
            # 全部落在两个来源内
            for ip in ips:
                assert ip.startswith("192.0.2.") or ip.startswith("203.0.113.")
        finally:
            ipdata.CF_IPS_CACHE = old_cf
            ipdata.EXT_IPS_CACHE = old_ext

    # 外部源缺失（文件不存在且下载失败）→ 仅官方源也能出样本
    with tempfile.TemporaryDirectory() as d:
        p_cf = os.path.join(d, "cf_ips.json")
        with open(p_cf, "w") as f:
            json.dump({"ts": time.time(), "v4": ["192.0.2.0/24"], "source": "official"}, f)
        old_cf, old_ext = ipdata.CF_IPS_CACHE, ipdata.EXT_IPS_CACHE
        orig_dl = ipdata._direct_download
        ipdata.CF_IPS_CACHE = Path(p_cf)
        ipdata.EXT_IPS_CACHE = Path(os.path.join(d, "nope.json"))
        ipdata._direct_download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            ips = ipdata.sample_cf_ips(10)
            assert len(ips) == 10 and all(i.startswith("192.0.2.") for i in ips)
        finally:
            ipdata.CF_IPS_CACHE = old_cf
            ipdata.EXT_IPS_CACHE = old_ext
            ipdata._direct_download = orig_dl


def test_is_known_ip():
    # 已知来源 = 官方段 ∪ 外部清单（不触网）
    cidrs = ["192.0.2.0/24"]
    ext = ["203.0.113.7"]
    assert ipdata.is_known_ip("192.0.2.9", cidrs, ext)      # 段内
    assert ipdata.is_known_ip("203.0.113.7", cidrs, ext)    # 清单内（段外）
    assert not ipdata.is_known_ip("203.0.113.8", cidrs, ext)  # 都不在
    assert not ipdata.is_known_ip("bad", cidrs, ext)


def test_pools_locate():
    pools.clear_all()
    pools.add("LAX", ["1.1.1.1", "1.1.1.2"])
    pools.add("SFO", ["2.2.2.1"])
    assert pools.locate("1.1.1.2") == "LAX"
    assert pools.locate("2.2.2.1") == "SFO"
    assert pools.locate("9.9.9.9") == ""
    pools.clear_all()


def test_geoip_groups():
    # colo_list_by_cc 返回 list（JSON 友好），中国系置顶
    groups = geoip.colo_list_by_cc()
    assert isinstance(groups, list)
    assert all(set(g) == {"cc", "cc_zh", "items"} for g in groups)
    # 中国（含港澳台）必须排在最前
    assert groups[0]["cc"] in ("CN", "HK", "MO", "TW")
    assert all(g["items"] for g in groups)
    assert all(i["code"] for g in groups for i in g["items"])


def test_direct_download_retries():
    # _direct_download 偶发中断（TLS EOF / 读超时）自动重试：前 2 次失败、第 3 次成功。
    # 直接调用 _direct_download（不经过 fetch_cf_ips），用 monkeypatch 的
    # urllib.request.build_opener 制造"前 N 次连接失败"。
    import urllib.request as _urllib
    retry_calls = {"n": 0}
    orig_build = _urllib.build_opener
    orig_sleep = time.sleep

    def fast_sleep(sec):  # 跳过真实退避等待
        pass

    class _FakeSock:
        def settimeout(self, t):
            pass
    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return b"192.0.2.0/24\n"

    def flaky_opener(req, timeout=30):
        # build_opener 返回一个 OpenerDirector（需有 .open 方法），
        # 而不是直接返回响应。
        class _Opener:
            def open(self, r, timeout=30):
                retry_calls["n"] += 1
                if retry_calls["n"] < 3:
                    raise ConnectionError("TLS EOF (第 %d 次)" % retry_calls["n"])
                return _FakeResp()
        return _Opener()

    try:
        _urllib.build_opener = lambda *a, **k: flaky_opener(None, timeout=30)
        time.sleep = fast_sleep
        out = ipdata._direct_download("https://example.invalid/cf", retries=3)
        assert out.strip() == "192.0.2.0/24"
        assert retry_calls["n"] == 3, f"期望 3 次（失败 2 + 成功 1），实际 {retry_calls['n']}"
    finally:
        time.sleep = orig_sleep
        _urllib.build_opener = orig_build

    # 重试耗尽仍失败 → 抛原异常
    retry_calls["n"] = 0
    def always_fail_opener(req, timeout=30):
        class _Opener:
            def open(self, r, timeout=30):
                retry_calls["n"] += 1
                raise ConnectionError("always down")
        return _Opener()
    try:
        _urllib.build_opener = lambda *a, **k: always_fail_opener(None, timeout=30)
        time.sleep = fast_sleep
        try:
            ipdata._direct_download("https://example.invalid/cf", retries=3)
            assert False, "should raise"
        except ConnectionError:
            pass
        assert retry_calls["n"] == 3, f"期望 3 次重试，实际 {retry_calls['n']}"
    finally:
        time.sleep = orig_sleep
        _urllib.build_opener = orig_build


def test_parse_cidr_lines():
    # 纯文本 CIDR 列表解析：忽略空行 / 注释 / 非法行
    out = ipdata._parse_cidr_lines("192.0.2.0/24\n\n# comment\nbadcidr\n 198.51.100.0/28 \n")
    assert out == ["192.0.2.0/24", "198.51.100.0/28"], out
    assert ipdata._parse_cidr_lines("") == []
    assert ipdata._parse_cidr_lines("# only comment\n") == []


def test_fetch_cf_ips_official():
    # 官方源 = cloudflare.com/ips-v4（CIDR 列表）
    assert ipdata.CF_IPS_URL == "https://www.cloudflare.com/ips-v4"
    assert ipdata.EXT_IPS_URL == "https://zip.cm.edu.kg/all.txt"
    orig = ipdata._direct_download
    _ip = ipdata
    _ip.CF_IPS_CACHE = Path("/nonexistent-fastcf-test/cf_ips.json")  # 强制绕过缓存
    _ip._direct_download = lambda url, timeout=30: "192.0.2.0/24\n198.51.100.0/28\n"
    try:
        out = _ip.fetch_cf_ips(force=True)
        assert out["v4"] == ["192.0.2.0/24", "198.51.100.0/28"]
        assert out["source"] == "https://www.cloudflare.com/ips-v4"
    finally:
        _ip._direct_download = orig
        _ip.CF_IPS_CACHE = orig_cache_path()
    # 下载失败且无旧缓存 → 抛异常
    _ip._direct_download = lambda url, timeout=30: (_ for _ in ()).throw(RuntimeError("nope"))
    _ip.CF_IPS_CACHE = Path("/nonexistent-fastcf-test/cf_ips.json")
    try:
        try:
            _ip.fetch_cf_ips(force=True)
            assert False, "should raise"
        except RuntimeError:
            pass
    finally:
        _ip._direct_download = orig
        _ip.CF_IPS_CACHE = orig_cache_path()


def test_fetch_external_ips_fallback():
    # 外部清单：下载失败但存在旧缓存 → 沿用旧缓存
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ext_ips.json")
        with open(p, "w") as f:
            json.dump({"ts": 0, "v4": ["1.2.3.4"], "source": "ext"}, f)  # ts=0 → 视为过期
        _ip = ipdata
        old = _ip.EXT_IPS_CACHE
        orig = _ip._direct_download
        _ip.EXT_IPS_CACHE = Path(p)
        _ip._direct_download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            out = _ip.fetch_external_ips()
            assert out["v4"] == ["1.2.3.4"], "应回退旧缓存"
        finally:
            _ip._direct_download = orig
            _ip.EXT_IPS_CACHE = old


def test_sources_status():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "cf_ips.json")
        p2 = os.path.join(d, "ext_ips.json")
        with open(p1, "w") as f:
            json.dump({"ts": 123, "v4": ["1.0.0.0/8"], "source": "s1"}, f)
        _ip = ipdata
        old1, old2 = _ip.CF_IPS_CACHE, _ip.EXT_IPS_CACHE
        _ip.CF_IPS_CACHE = Path(p1)
        _ip.EXT_IPS_CACHE = Path(p2)
        try:
            st = _ip.sources_status()
            assert st["official"] == {"n": 1, "ts": 123, "source": "s1"}
            assert st["external"] == {"n": 0, "ts": 0, "source": ""}  # 无缓存
        finally:
            _ip.CF_IPS_CACHE = old1
            _ip.EXT_IPS_CACHE = old2


def orig_cache_path():
    from pathlib import Path as P
    import os
    return P(os.environ.get("FASTCF_HOME", str(P.home() / ".fastcf"))) / "cf_ips.json"


def test_sample_cf_ips_large_segment():
    # 官方段采样：/24、/23 混合小段都能覆盖，采样出的 IP 必须全部落在给定段内
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cf_ips.json")
        p_ext = os.path.join(d, "ext_missing.json")
        with open(p, "w") as f:
            json.dump({
                "ts": time.time(),
                "v4": ["192.0.2.0/24", "198.51.100.0/23", "203.0.113.0/24"],  # 混合段
            }, f)
        p_old, p_ext_old = ipdata.CF_IPS_CACHE, ipdata.EXT_IPS_CACHE
        orig_dl = ipdata._direct_download
        ipdata.CF_IPS_CACHE = Path(p)
        ipdata.EXT_IPS_CACHE = Path(p_ext)
        ipdata._direct_download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            ips = ipdata.sample_cf_ips(30, use_v6=False)
        finally:
            ipdata.CF_IPS_CACHE = p_old
            ipdata.EXT_IPS_CACHE = p_ext_old
            ipdata._direct_download = orig_dl
        assert len(ips) == 30, "外部源缺失时应由官方源补满"
        nets = [ipaddress.ip_network(c) for c in ["192.0.2.0/24", "198.51.100.0/23", "203.0.113.0/24"]]
        for ip in ips:
            a = ipaddress.ip_address(ip)
            assert any(a in n for n in nets), f"{ip} 不在段内"


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
    # 回归：官方段中存在比目标更小的子网（如 /28），必须保留
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cf_ips.json")
        p_ext = os.path.join(d, "ext_missing.json")
        with open(p, "w") as f:
            json.dump({
                "ts": time.time(),
                "v4": ["192.0.2.0/24", "198.51.100.0/28"],   # /28 更小，必须保留
            }, f)
        p_old, p_ext_old = ipdata.CF_IPS_CACHE, ipdata.EXT_IPS_CACHE
        orig_dl = ipdata._direct_download
        ipdata.CF_IPS_CACHE = Path(p)
        ipdata.EXT_IPS_CACHE = Path(p_ext)
        ipdata._direct_download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            ips = ipdata.sample_cf_ips(10, use_v6=False)
        finally:
            ipdata.CF_IPS_CACHE = p_old
            ipdata.EXT_IPS_CACHE = p_ext_old
            ipdata._direct_download = orig_dl
        assert len(ips) == 10
        nets = [ipaddress.ip_network(c) for c in ["192.0.2.0/24", "198.51.100.0/28"]]
        for ip in ips:
            a = ipaddress.ip_address(ip)
            assert any(a in n for n in nets), f"{ip} 不在段内"


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
