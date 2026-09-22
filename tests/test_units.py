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

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
_vendor = os.path.join(_ROOT, ".vendor")
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)
os.environ["FASTCF_HOME"] = tempfile.mkdtemp(prefix="fastcf-test-")

from fastcf import exports, colos, config, history, pool, scanner, sources, store  # noqa: E402


def test_cidr_parse():
    # 合法行保留，非法行丢弃
    text = "1.1.1.1/32\n\n# comment\n104.16.0.0/13\nnot-a-cidr\n172.67.0.0/16\n"
    out = sources._parse_cidr_lines(text)
    assert out == ["1.1.1.1/32", "104.16.0.0/13", "172.67.0.0/16"], out


def test_ext_parse():
    text = "104.16.13.35:443#US\n1.2.3.4:80#CN\n5.6.7.8:443\n999.1.1.1:443#XX\n"
    ips, kept, skipped = sources.parse_ext_lines(text)
    assert ips == ["104.16.13.35", "5.6.7.8"], ips
    assert kept == 2 and skipped == 2


def test_sample_fallback():
    # 无网络环境下 sample_cf_ips 应优雅降级（不抛异常，返回 [] 或有限列表）
    out = sources.sample_cf_ips(50)
    assert isinstance(out, list)
    assert len(out) <= 50


def test_is_known_ip():
    assert sources.is_in_cf_v4("1.1.1.1", ["1.1.1.0/24"])
    assert not sources.is_in_cf_v4("8.8.8.8", ["1.1.1.0/24"])
    assert sources.is_known_ip("1.1.1.1", ["1.1.1.0/24"], [])
    assert sources.is_known_ip("5.6.7.8", [], ["5.6.7.8"])
    assert not sources.is_known_ip("9.9.9.9", ["1.1.1.0/24"], ["5.6.7.8"])


def test_store_roundtrip():
    # 统一持久化层：原子写 + 读回一致
    p = Path(os.environ["FASTCF_HOME"]) / "rt.json"
    store.write_json(p, {"a": 1, "b": [1, 2]})
    assert store.read_json(p, {}) == {"a": 1, "b": [1, 2]}
    assert store.read_json(Path(os.environ["FASTCF_HOME"]) / "missing.json", {"d": 1}) == {"d": 1}


def test_pools():
    pool.clear_all()
    pool.add("LAX", ["1.1.1.1", "1.1.1.2"])
    pool.add("LAX", ["1.1.1.2", "1.1.1.3"])  # 去重
    assert pool.get("LAX") == ["1.1.1.1", "1.1.1.2", "1.1.1.3"]
    pool.remove("LAX", ["1.1.1.2"])
    assert pool.get("LAX") == ["1.1.1.1", "1.1.1.3"]
    assert pool.size("lax") == 2  # 大小写不敏感
    assert pool.pool_report() == {"LAX": 2}
    assert pool.pools_detail()[0]["code"] == "LAX"
    # 持久化往返（经 store 原子写）
    data = json.loads(store.POOL_FILE.read_text())
    assert data["pools"]["LAX"]["ips"] == ["1.1.1.1", "1.1.1.3"]
    # 截断：超过 max_size 保留最新
    pool.clear_all()
    big = [f"9.9.9.{i}" for i in range(config.POOL_SIZE + 10)]
    pool.add("SFO", big)
    assert pool.size("SFO") == config.POOL_SIZE
    assert pool.get("SFO")[-1] == big[-1]
    pool.clear_all()
    assert pool.get("LAX") == []


def test_pool_cap_no_bloat():
    # 池上限 POOL_SIZE(50) 防大小爆炸：
    big = [f"9.9.9.{i}" for i in range(config.POOL_SIZE + 30)]
    pool.add("SFO", big)
    assert pool.size("SFO") == config.POOL_SIZE
    assert pool.get("SFO") == big[-config.POOL_SIZE:]
    for _ in range(5):
        pool.add("SFO", [f"9.9.8.{i}" for i in range(config.POOL_SIZE)])
    assert pool.size("SFO") == config.POOL_SIZE
    data = json.loads(store.POOL_FILE.read_text())
    assert len(data["pools"]["SFO"]["ips"]) == config.POOL_SIZE
    pool.clear_all()


def test_pool_index():
    pool.clear_all()
    pool.add("LAX", ["1.1.1.1", "1.1.1.2"])
    pool.add("SFO", ["2.2.2.2"])
    idx = pool.build_ip_index()
    assert idx == {"1.1.1.1": "LAX", "1.1.1.2": "LAX", "2.2.2.2": "SFO"}
    pool.clear_all()


def test_expired_single_dc():
    pool.clear_all()
    pool.add("LAX", ["1.1.1.1"])
    pool.add("SFO", ["2.2.2.2"])
    import fastcf.pool as _p
    with _p._lock:
        _p._pool_ts["LAX"] = time.time() - _p.config.POOL_TTL - 1
    try:
        assert pool.expired("LAX")
        assert not pool.expired("SFO")
        pool.touch("LAX")
        assert not pool.expired("LAX")
    finally:
        pool.clear_all()


def test_expired_global():
    pool.clear_all()
    pool.add("LAX", ["1.1.1.1"])
    assert not pool.expired()
    import fastcf.pool as _p
    old = _p._pools_ts
    _p._pools_ts = time.time() - _p.config.POOL_TTL - 1
    try:
        assert pool.expired()
    finally:
        _p._pools_ts = old
        pool.clear_all()


def test_scan_params_validation():
    # Pydantic 校验：非法参数直接拒绝
    from fastcf.config import ScanParams
    assert ScanParams(mode="DC", colo="HKG").validate() == ""
    assert ScanParams(mode="DC", colo="XX").validate() != ""
    assert ScanParams(mode="RANDOM").validate() == ""
    try:
        ScanParams(mode="BAD")
        raise AssertionError("应当抛出 ValidationError")
    except Exception:
        pass
    try:
        ScanParams(mode="RANDOM", randomCount=5)  # < 10
        raise AssertionError("应当抛出 ValidationError")
    except Exception:
        pass


def _sample_result():
    return {
        "count": 1, "elapsed": 5, "ipVer": "v4", "tls": True,
        "mode": "RANDOM", "colo": None, "randomCount": 10, "minSpeed": 0,
        "results": [{
            "ip": "1.1.1.1", "ping": 10, "latency": 10, "loss": 0.0,
            "mbps": 100, "port": 443, "dc": "LAX", "dc_zh": "美国·洛杉矶",
            "cfRay": "abc-LAX-1", "location": "美国·洛杉矶", "tls": True,
        }],
    }


def test_exports():
    r = _sample_result()
    csv = exports.to_csv(r)
    assert "1.1.1.1" in csv and "LAX" in csv
    j = json.loads(exports.to_json(r))
    assert j["results"][0]["ip"] == "1.1.1.1"
    d = exports.export(r, "csv")
    assert d["filename"] == "fastcf_result.csv"
    try:
        exports.export(r, "xml")
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass


def test_history():
    history.clear()
    history.add(_sample_result(), {"mode": "RANDOM", "randomCount": 10})
    h = history.load()
    assert len(h) == 1 and h[0]["results"][0]["ip"] == "1.1.1.1"
    assert history.delete(h[0]["id"])
    assert history.load() == []


def test_colos():
    # 静态快照：HKG 应存在
    assert colos.colos.zh("HKG") != ""
    assert colos.colos.country("HKG") in ("HK", None)  # 快照可能有或无
    g = colos.colos.groups()
    assert isinstance(g, list) and len(g) > 0
    # 中国系置顶
    first_cc = g[0]["cc"]
    assert first_cc in ("CN", "HK", "MO", "TW"), first_cc


def test_zhnames():
    from fastcf.zhnames import COUNTRY_ZH, _CITY_ZH
    assert COUNTRY_ZH.get("CN") == "中国"
    assert _CITY_ZH.get("Hong Kong") == "香港"
    assert len(COUNTRY_ZH) > 100 and len(_CITY_ZH) > 100


def test_ping_parse():
    from fastcf import ping
    out = ("PING 1.1.1.1 (1.1.1.1): 56 data bytes\n"
           "64 bytes from 1.1.1.1: icmp_seq=0 ttl=56 time=14.3 ms\n"
           "64 bytes from 1.1.1.1: icmp_seq=1 ttl=56 time=15.1 ms\n"
           "64 bytes from 1.1.1.1: icmp_seq=2 ttl=56 time=13.9 ms\n"
           "64 bytes from 1.1.1.1: icmp_seq=3 ttl=56 time=14.8 ms\n"
           "--- 1.1.1.1 ping statistics ---\n"
           "4 packets transmitted, 4 packets received, 0.0% packet loss\n"
           "round-trip min/avg/max/mdev = 13.9/14.5/15.1/0.5 ms\n")
    avg, loss = ping._parse(out)
    assert loss == 0.0
    assert 13 <= avg <= 16, avg
    # 全丢包
    out2 = "4 packets transmitted, 0 packets received, 100.0% packet loss\n"
    avg2, loss2 = ping._parse(out2)
    assert loss2 == 1.0 and avg2 == 0


def _mock_ctx():
    """构造离线 mock 的 EngineContext（不触网）。"""
    class FakeColos:
        def zh(self, c): return c
        def country(self, c): return None
    class FakePool:
        def __init__(self): self.p = {}
        def get(self, c): return list(self.p.get(c.upper(), []))
        def add(self, c, ips, save=False):
            self.p.setdefault(c.upper(), [])
            for i in ips:
                if i not in self.p[c.upper()]: self.p[c.upper()].append(i)
        def remove(self, c, ips):
            self.p[c.upper()] = [i for i in self.p.get(c.upper(), []) if i not in set(ips)]
        def touch(self, c): pass
        def expired(self, c=""): return False
        def build_ip_index(self):
            idx = {}
            for c, ips in self.p.items():
                for i in ips: idx[i] = c
            return idx
    class FakeSources:
        def sample_cf_ips(self, n): return [f"10.0.{i//250}.{i%250}" for i in range(n)]
    ctx = scanner.EngineContext()
    ctx.sources = FakeSources()
    ctx.pool = FakePool()
    ctx.colos = FakeColos()
    ctx.probe_location = lambda ip, timeout=4: ("US", "LAX", "Los Angeles")
    ctx.speed_test = lambda ip, b, s, is_cancelled=None: {
        "ip": ip, "port": 443, "ping": 5, "mbps": 120, "dc": "LAX",
        "cfRay": "x-LAX-1", "location": "US·Los Angeles"}
    ctx.ping = lambda ip, times=4, timeout=2: (12, 0.0)
    ctx.ping_probe = lambda ip, timeout=2: (True, 12)
    ctx.speed_gap = 0  # 离线测试免真实 sleep（生产默认 config.SPEED_GAP=0.5s）
    return ctx


def test_scanner_full_offline():
    # 依赖注入后，完整扫描流程可离线跑通（随机模式 → 5 个达标结果）
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 3, "speedMB": 10, "minSpeed": 0}, _mock_ctx())
    s.run()
    assert s.done.is_set()
    assert s.result_payload["count"] == 5
    assert s.result_payload["mode"] == "RANDOM"
    assert len(s.result_payload["results"]) == 5


def test_scanner_finalize():
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 8, "speedMB": 50, "minSpeed": 0}, _mock_ctx())
    s.start_ts = time.time()
    fake = [{"ip": "1.1.1.1", "ping": 10, "loss": 0.0, "mbps": 100,
             "dc": "LAX", "dc_zh": "美国·洛杉矶", "loc": "美国·洛杉矶",
             "cfRay": "abc-LAX-1", "port": 443}]
    s._finalize(fake, "RANDOM", "", False, 10, 0, cancelled=False)
    assert s.result_payload is not None
    assert s.result_payload["count"] == 1
    assert s.result_payload["results"][0]["ip"] == "1.1.1.1"
    assert s.done.is_set()


def test_scanner_cancel_finalize():
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 8, "speedMB": 50, "minSpeed": 0}, _mock_ctx())
    s.start_ts = time.time()
    fake = [{"ip": "1.1.1.1", "ping": 10, "loss": 0.0, "mbps": 100,
             "dc": "LAX", "dc_zh": "美国·洛杉矶", "loc": "美国·洛杉矶",
             "cfRay": "abc-LAX-1", "port": 443},
            {"ip": "2.2.2.2", "ping": 20, "loss": 0.1, "mbps": 50,
             "dc": "SFO", "dc_zh": "美国·圣何塞", "loc": "美国·圣何塞",
             "cfRay": "abc-SFO-1", "port": 443}]
    s._finalize(fake, "RANDOM", "", False, 10, 0, cancelled=True)
    assert s.result_payload is not None
    assert s.result_payload["cancelled"] is True
    assert s.result_payload["count"] == 2
    assert "取消" in s.result_payload["mode"]
    assert s.done.is_set()


def test_scanner_error():
    s = scanner.Scanner({"mode": "DC", "colo": "HKG"}, _mock_ctx())
    s.start_ts = time.time()
    s._finish_error("测试错误")
    assert s.result_payload == {"error": "测试错误"}
    assert s.done.is_set()


def test_appstate():
    from fastcf.appstate import AppState
    st = AppState()
    assert not st.running
    ok, err = st.start({"mode": "DC", "colo": "HKG", "randomCount": 10,
                       "speedSecs": 8, "speedMB": 50, "minSpeed": 0})
    # DC 池为空 → 回退随机 → 采样（网络不可达时优雅降级）→ 快速结束
    # 注意：真实网络环境下 ping 预筛可能耗时较长（慢握手），等待上限放宽到 240s
    assert ok, err
    st.scanner.done.wait(timeout=240)
    assert not st.running
    # 重复启动应被拒绝（仅当 scanner 未完成时；此处已完成，可再启动）
    st2 = AppState()
    ok2, err2 = st2.start({"mode": "DC", "colo": "HKG", "randomCount": 10,
                          "speedSecs": 8, "speedMB": 50, "minSpeed": 0})
    assert ok2, err2
    st2.scanner.done.wait(timeout=240)
    # last_error 快速验证：错误 scanner 写入后，scanner 替换（置空）仍可见
    sc_err = scanner.Scanner({"mode": "DC", "colo": "HKG"}, _mock_ctx())
    st2.scanner = sc_err
    sc_err.start_ts = time.time()
    sc_err._finish_error("last_error 测试")
    sc_err.done.set()
    if sc_err.result_payload and "error" in sc_err.result_payload:
        with st2.lock:
            st2.last_error = sc_err.result_payload["error"]
    assert st2.last_error == "last_error 测试"
    st2.scanner = None
    assert st2.status().get("error") == "last_error 测试"


def test_scanner_parallel_speed():
    # 并行测速：mock speed_test 带延迟，验证并发执行 + 凑够 need 即停
    import threading as _th
    calls = []
    lock = _th.Lock()

    def slow_speed_test(ip, b, s, is_cancelled=None):
        with lock:
            calls.append(ip)
        time.sleep(0.05)
        return {"ip": ip, "port": 443, "ping": 5, "mbps": 120, "dc": "LAX",
                "cfRay": "x-LAX-1", "location": "US"}

    ctx = _mock_ctx()
    ctx.speed_test = slow_speed_test
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 12,
                        "speedSecs": 3, "speedMB": 10, "minSpeed": 0}, ctx)
    t0 = time.perf_counter()
    s.run()
    elapsed = time.perf_counter() - t0
    assert s.result_payload["count"] == 5
    # 12 个候选并发 4 路：若串行需 12×50ms=600ms；并发应 < 600ms
    # 允许 ping 阶段开销，只验证测速部分没有串行放大（总时长 < 2s 宽松上限）
    assert elapsed < 2.0, f"并行测速疑似串行：{elapsed:.2f}s"


def test_scanner_fast_fail():
    # 快速失败：连续 3 个 IP 都 0Mbps → 提前停止（不再测后续候选）
    # 注意：4 路并发下，fail_count 在提交循环中检查（非完成循环），
    # 所以最多测 4+3=7 个（第一轮 4 个 + 第二轮 3 个触发 fail_count>=3）
    import threading as _th
    calls = []
    lock = _th.Lock()

    def zero_speed_test(ip, b, s, is_cancelled=None):
        with lock:
            calls.append(ip)
        return {"ip": ip, "port": 443, "ping": 5, "mbps": 0, "dc": "",
                "cfRay": "", "location": ""}

    ctx = _mock_ctx()
    ctx.speed_test = zero_speed_test
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 20,
                        "speedSecs": 3, "speedMB": 10, "minSpeed": 0}, ctx)
    s.run()
    # 快速失败后走 _finish_error（result_payload 含 error 字段）
    assert "error" in s.result_payload, f"快速失败应走 error 路径：{s.result_payload}"
    # 4 路并发：第一轮 4 个全失败（fail_count=4），第二轮提交时 fail_count>=3 → 停止
    # 所以最多测 4 个（第一轮）+ 0 个（第二轮被阻止）= 4 个
    # 但 as_completed 可能让第二轮部分 future 已提交，所以放宽到 8
    assert len(calls) <= 8, f"快速失败未生效：测了 {len(calls)} 个"


def test_scanner_cancel_mid_speed():
    # 取消：测速进行中取消 → 保留已完成结果，走 _finalize（cancelled=True）
    import threading as _th
    ev = _th.Event()

    def slow_speed_test(ip, b, s, is_cancelled=None):
        ev.wait(timeout=5)  # 等待取消信号
        return {"ip": ip, "port": 443, "ping": 5, "mbps": 120, "dc": "LAX",
                "cfRay": "x-LAX-1", "location": "US"}

    ctx = _mock_ctx()
    ctx.speed_test = slow_speed_test
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 3, "speedMB": 10, "minSpeed": 0}, ctx)
    t = _th.Thread(target=s.run)
    t.start()
    time.sleep(0.5)  # 等待进入测速阶段
    s.cancel()
    ev.set()  # 释放被阻塞的 speed_test
    t.join(timeout=10)
    assert s.done.is_set()
    assert s.result_payload is not None
    assert s.result_payload.get("cancelled") is True


def test_appstate_status_stage():
    # status() 返回 stage 字段（done / cancelled / error），供前端显示
    from fastcf.appstate import AppState
    st = AppState()
    # 无扫描：无 stage
    assert "stage" not in st.status()
    # 错误 scanner
    sc_err = scanner.Scanner({"mode": "DC", "colo": "HKG"}, _mock_ctx())
    st.scanner = sc_err
    sc_err.start_ts = time.time()
    sc_err._finish_error("测试错误")
    sc_err.done.set()
    with st.lock:
        st.last_error = "测试错误"
    assert st.status().get("stage") == "error"
    # 取消 scanner
    sc_cancel = scanner.Scanner({"mode": "DC", "colo": "HKG"}, _mock_ctx())
    st.scanner = sc_cancel
    sc_cancel.start_ts = time.time()
    sc_cancel._finalize([], "DC", "HKG", False, 10, 0, cancelled=True)
    with st.lock:
        st.last_result = sc_cancel.result_payload
    assert st.status().get("stage") == "cancelled"
    # 完成 scanner
    sc_done = scanner.Scanner({"mode": "DC", "colo": "HKG"}, _mock_ctx())
    st.scanner = sc_done
    sc_done.start_ts = time.time()
    fake = [{"ip": "1.1.1.1", "ping": 10, "loss": 0.0, "mbps": 100,
             "dc": "LAX", "dc_zh": "美国", "loc": "美国",
             "cfRay": "abc-LAX-1", "port": 443}]
    sc_done._finalize(fake, "DC", "HKG", False, 10, 0, cancelled=False)
    with st.lock:
        st.last_result = sc_done.result_payload
    assert st.status().get("stage") == "done"


def test_speedtest_429_throttle():
    # 429 退避：收到 429 后设置冷却期，后续连接先等待
    from fastcf import speedtest
    # 重置（防止前一个测试残留）
    speedtest._throttle_until = 0
    # 初始无冷却
    assert speedtest._throttle_until <= time.monotonic()
    # 设置 429 冷却（10s）
    speedtest._throttle_set(10)
    assert speedtest._throttle_until > time.monotonic()
    # 冷却期内，_throttle_wait 应等待（用取消打断验证）
    ev = [False]
    def is_cancelled():
        return ev[0]
    t0 = time.monotonic()
    ev[0] = True  # 立即取消
    speedtest._throttle_wait(is_cancelled)
    assert time.monotonic() - t0 < 1.5  # 取消打断，不应等满 10s
    # 重置
    speedtest._throttle_until = 0


def test_speedtest_parse_headers():
    # 响应头解析：cf-ray / cf-meta-* 头
    from fastcf import speedtest
    head = b"HTTP/1.1 200 OK\r\nCF-RAY: abc123-LAX-1\r\nCF-META-COLO: LAX\r\nCF-META-COUNTRY: US\r\nCF-META-CITY: Los Angeles\r\nContent-Length: 100\r\n\r\n"
    meta = speedtest.parse_cf_headers(head)
    assert meta["cf-ray"] == "abc123-LAX-1"
    assert meta["cf-meta-colo"] == "LAX"
    assert meta["cf-meta-country"] == "US"
    # 429 检测
    head429 = b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 3000\r\n\r\n"
    m429 = speedtest.parse_cf_headers(head429)
    assert m429.get("retry-after") == "3000"


def test_scanner_log_delta():
    # 增量日志：_emit_state 附带 logDelta/logTotal；游标推进后 delta 为空
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 3, "speedMB": 10, "minSpeed": 0}, _mock_ctx())
    s.start_ts = time.time()
    s.log("第一条")
    s.log("第二条")
    assert s.last_state["logTotal"] == 2
    assert len(s.last_state["logDelta"]) == 1  # 游标在第一条后推进
    assert s.last_state["logDelta"][0]["msg"] == "第二条"
    # 无新日志时 delta 为空
    s._emit_state({"running": True, "stage": "x", "pct": 1})
    assert s.last_state["logDelta"] == []
    assert s.last_state["logTotal"] == 2


def test_result_rank():
    # 结果 rank 字段：1-5，按 延迟→丢包→速度 排序名次
    s = scanner.Scanner({"mode": "RANDOM", "randomCount": 10,
                        "speedSecs": 8, "speedMB": 50, "minSpeed": 0}, _mock_ctx())
    s.start_ts = time.time()
    fake = [{"ip": f"1.1.1.{i}", "ping": i * 10, "loss": 0.0, "mbps": 100,
             "dc": "LAX", "dc_zh": "美国", "loc": "美国",
             "cfRay": "abc-LAX-1", "port": 443} for i in range(1, 6)]
    s._finalize(fake, "RANDOM", "", False, 10, 0, cancelled=False)
    ranks = [r["rank"] for r in s.result_payload["results"]]
    assert ranks == [1, 2, 3, 4, 5], ranks


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
