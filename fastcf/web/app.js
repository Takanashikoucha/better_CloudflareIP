/* FastCF 前端逻辑 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const COUNTRIES = [
  ["CN", "中国大陆"], ["HK", "中国香港"], ["MO", "中国澳门"], ["TW", "中国台湾"],
  ["JP", "日本"], ["KR", "韩国"], ["SG", "新加坡"], ["MY", "马来西亚"],
  ["US", "美国"], ["GB", "英国"], ["DE", "德国"], ["NL", "荷兰"],
  ["FR", "法国"], ["AU", "澳大利亚"], ["CA", "加拿大"],
  ["RU", "俄罗斯"], ["IN", "印度"], ["TH", "泰国"], ["VN", "越南"],
  ["ID", "印度尼西亚"], ["PH", "菲律宾"], ["BR", "巴西"], ["ZA", "南非"],
];
const DEFAULT_ON = ["CN", "HK", "MO", "TW", "JP", "KR", "SG", "MY"];

let lastResult = null;
let lastResultSource = "latest";  // "latest" | {id} — 当前展示结果对应的导出来源
let sse = null;
let logN = 0;
let resSortKey = "ping";
const state = {
  ipVer: "v4",
  tls: true,
  count: 5,
  colo: "",          // 指定 DC，空 = 自动就近
  countries: new Set(DEFAULT_ON),
  sample: 150,
  speedSecs: 8,
  speedMB: 50,
  bw: 100,
  minSpeed: 0,       // 下载速度下限（Mbps）；>0 时凑够 count 个达标 IP 即停
};

/* ── 主题 ── */
function initTheme() {
  const saved = localStorage.getItem("fastcf-theme") || "light";
  setTheme(saved, true);
  $("#btnTheme").onclick = () => setTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
}
function setTheme(t, silent) {
  document.body.dataset.theme = t;
  localStorage.setItem("fastcf-theme", t);
  $("#btnTheme").textContent = t === "dark" ? "☀️" : "🌙";
  if (!silent) toast(t === "dark" ? "已切换深色模式" : "已切换浅色模式");
}

/* ── 控件绑定 ── */
function initControls() {
  // 分段按钮
  const bindSeg = (el, key, parse) => {
    $$(el + " button").forEach(b => b.onclick = () => {
      $$(el + " button").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      state[key] = parse ? parse(b.dataset.v) : b.dataset.v;
    });
  };
  // 数值字段 → state（params() 直接读 state，历史复用即可生效）
  const numBind = (id, key, min, max, dflt) => {
    const el = $(id);
    el.onchange = () => {
      let v = parseInt(el.value, 10);
      if (isNaN(v)) v = dflt;
      el.value = Math.max(min, Math.min(max, v));
      state[key] = parseInt(el.value, 10);
    };
    state[key] = parseInt(el.value, 10) || dflt;
  };
  numBind("#inSample", "sample", 20, 3000, 150);
  numBind("#inSecs", "speedSecs", 3, 60, 8);
  numBind("#inMB", "speedMB", 10, 1000, 50);
  numBind("#inBW", "bw", 1, 10000, 100);
  numBind("#inMinSpeed", "minSpeed", 0, 10000, 0);
  bindSeg("#segVer", "ipVer");
  // tls 按钮 data-v 是 "1"/"0"，统一存布尔，避免 params() 里再做字符串比较
  bindSeg("#segTls", "tls", v => v === "1");
  bindSeg("#segCount", "count", v => parseInt(v, 10));

  // DC 节点下拉框（选中后隐藏国家组 chip，恢复自动就近时显示）
  $("#selDC").onchange = (e) => {
    state.colo = e.target.value;
    syncCountryChipVisibility();
  };

  syncCountryChipVisibility();

  // 国家 chips（去重）
  const seen = new Set();
  const chipsEl = $("#countryChips");
  for (const [code, name] of COUNTRIES) {
    if (seen.has(code)) continue;
    seen.add(code);
    const c = document.createElement("span");
    c.className = "chip" + (state.countries.has(code) ? " on" : "");
    c.textContent = name;
    c.onclick = () => {
      if (state.countries.has(code)) state.countries.delete(code);
      else state.countries.add(code);
      c.classList.toggle("on");
    };
    chipsEl.appendChild(c);
  }

  // 结果表（CSV 下载走后端 /api/export，复制 IP 走剪贴板）
  $("#btnDlCsv").onclick = () => {
    fetch("/api/export?" + currentSource() + "&fmt=csv")
      .then(async r => { if (!r.ok) return toast("CSV 导出失败"); downloadBlob(await r.blob(), "fastcf_result.csv"); })
      .catch(() => toast("CSV 导出失败"));
  };
  $("#resSort").onchange = (e) => { resSortKey = e.target.value; if (lastResult) showResults(lastResult); };
  // 表头点击排序
  $$("#resTable th.sortable").forEach(th => th.onclick = () => {
    resSortKey = th.dataset.k;
    $("#resSort").value = resSortKey;
    $$("#resTable th.sortable").forEach(x => x.classList.toggle("on", x === th));
    if (lastResult) showResults(lastResult);
  });

  // IP 池 / 信息弹窗
  $("#btnPools").onclick = openPools;
  $("#btnClosePools").onclick = () => $("#poolsModal").style.display = "none";
  $("#btnInfo").onclick = openInfo;
  $("#btnCloseInfo").onclick = () => $("#infoModal").style.display = "none";
  $("#btnPoolAdd").onclick = poolAdd;
  $("#btnPoolClearAll").onclick = () => {
    if (!confirm("确定清空全部 IP 池？")) return;
    fetch("/api/pools", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "clear_all" }) })
      .then(r => r.json()).then(d => { if (d.ok) { toast(`已清空 ${d.removed} 个 IP`); loadPools(); loadColos(); } });
  };
  document.querySelectorAll(".modal-mask").forEach(m => m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; }));

  // 选项卡
  $$(".tabbar button").forEach(b => b.onclick = () => {
    $$(".tabbar button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    $$(".tab").forEach(t => t.classList.remove("show"));
    $("#tab-" + b.dataset.tab).classList.add("show");
  });

  // 扫描 / 取消
  $("#btnScan").onclick = startScan;
  $("#btnCancel").onclick = async () => {
    await fetch("/api/cancel", { method: "POST" });
  };
  $("#btnClearHist").onclick = async () => {
    if (!confirm("确定清空全部历史记录？")) return;
    await fetch("/api/history", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "clear" }) });
    loadHistory();
  };
}

/* 国家组 chip 可见性：指定 DC 或全局随机时隐藏（它们不依赖国家组） */
function syncCountryChipVisibility() {
  const wrap = $("#countryChips");
  if (wrap) wrap.style.display = state.colo ? "none" : "";
  const lab = wrap && wrap.closest(".field");
  if (lab) lab.style.display = state.colo ? "none" : "";
}

/* ── DC 节点选择：国家级为主，中国展开城市 ── */
async function loadColos() {
  const sel = $("#selDC");
  if (!sel) return;
  const cur = sel.value;
  // 始终保留：自动就近 + 全局随机（loadColos 被 30s 定时调用，不重建会丢失）
  sel.innerHTML =
    '<option value="">— 自动就近（按国家组）—</option>' +
    '<option value="RANDOM">🌐 全局随机（全 CF 池）</option>';
  try {
    const groups = await fetch("/api/colos").then(r => r.json());
    for (const g of groups) {
      const og = document.createElement("optgroup");
      og.label = `${g.cc_zh} (${g.cc})`;
      if (g.cc === "CN") {
        // 中国大陆：展开列出城市级节点，可精确到单点
        for (const c of g.items) {
          const o = document.createElement("option");
          o.value = c.code;
          o.textContent = c.name.replace(/^中国·?/, "") + (c.pool ? `  · 池 ${c.pool} IP` : "");
          og.appendChild(o);
        }
      } else {
        // 其他国家：只选国家（代表该国全部节点）
        const o = document.createElement("option");
        o.value = "CC:" + g.cc;
        o.textContent = `${g.cc_zh}（${g.count} 个节点）` + (g.pool ? `  · 池 ${g.pool} IP` : "");
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
  } catch { /* 加载失败不影响使用 */ }
  // 恢复选择（含 RANDOM / 国家 / 具体 DC）
  if (cur && Array.from(sel.options).some(o => o.value === cur)) sel.value = cur;
}

/* ── SSE 日志 ── */
function openSSE() {
  if (sse) sse.close();
  logN = 0;
  $("#logBox").innerHTML = "";
  sse = new EventSource("/api/stream");
  sse.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      if (d.type === "state") onState(d);
    } catch {}
  };
  sse.onerror = () => { /* 连接关闭或断开，扫描结束时会自然重连 */ };
}
function onState(d) {
  $("#stageName").textContent = stageName(d.stage);
  $("#stagePct").textContent = d.pct + "%";
  $("#stageFill").style.width = d.pct + "%";
  $("#stageDetail").textContent = d.detail || "";
  $("#stageElapsed").textContent = "耗时 " + (d.elapsed || 0) + "s";
  // 日志：只追加新增行
  const logs = d.logs || [];
  const box = $("#logBox");
  if (logs.length > logN) {
    const frag = document.createDocumentFragment();
    for (let i = logN; i < logs.length; i++) {
      const l = logs[i];
      const div = document.createElement("div");
      div.className = "line " + (l.level || "");
      div.innerHTML = '<span class="ts">' + l.ts + '</span>' + esc(l.msg);
      frag.appendChild(div);
    }
    box.appendChild(frag);
    box.scrollTop = box.scrollHeight;
    logN = logs.length;
  }
  if (d.stage === "done" || d.stage === "error") {
    setTimeout(() => {
      fetch("/api/status").then(r => r.json()).then(s => {
        if (s.result) { lastResultSource = "latest"; showResults(s.result); loadColos(); loadStatus(); }
        else if (s.error) { toast("扫描失败：" + s.error); }
        setRunningUI(false);
      });
    }, 300);
  }
}
function stageName(s) {
  return { prepare: "准备中", geo: "加载地理库", sample: "采样 IP", rtt: "RTT 验证", speed: "带宽测速", done: "扫描完成", error: "出错" }[s] || s;
}
function esc(s) { return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ── 扫描 ── */
function params() {
  return {
    ipVer: state.ipVer,
    tls: state.tls === true,
    count: parseInt(state.count, 10) || 5,
    colo: state.colo,
    countries: Array.from(state.countries),
    sample: state.sample || 150,
    speedSecs: state.speedSecs || 8,
    speedMB: state.speedMB || 50,
    bw: state.bw || 100,
    minSpeed: state.minSpeed || 0,
  };
}
async function startScan() {
  const p = params();
  const ok = await fetch("/api/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
  const r = await ok.json();
  if (r.error) return toast(r.error);
  setRunningUI(true);
  openSSE();
}
function setRunningUI(on) {
  $("#btnScan").disabled = on;
  $("#btnScan").textContent = on ? "⏳ 扫描中..." : "🚀 开始优选";
  $("#btnCancel").style.display = on ? "" : "none";
  if (on) $("#progressCard").scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ── 结果展示 ── */
function showResults(res) {
  lastResult = res;
  $("#resultArea").style.display = "";
  $$(".tabbar button").forEach((b, i) => b.classList.toggle("on", i === 0));
  $$(".tab").forEach(t => t.classList.toggle("show", t.id === "tab-results"));
  let rows = (res.results || []).slice();
  const bw = 100; // Mbps 参考
  // 排序：ping 升序 / loss 升序 / mbps 降序（i 表示保持扫描排名）
  if (resSortKey === "mbps") rows.sort((a, b) => (b.mbps || 0) - (a.mbps || 0));
  else if (resSortKey === "loss") rows.sort((a, b) => (a.loss ?? 1) - (b.loss ?? 1));
  else if (resSortKey === "ping") rows.sort((a, b) => (a.latency ?? a.ping ?? 1e9) - (b.latency ?? b.ping ?? 1e9));
  else if (resSortKey === "dc") rows.sort((a, b) => String(a.dc || "").localeCompare(String(b.dc || "")));
  else if (resSortKey === "proto") rows.sort((a, b) => (a.port || 0) - (b.port || 0));
  const body = $("#resBody");
  body.innerHTML = "";
  $("#resEmpty").style.display = rows.length ? "none" : "";
  rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    const rankCls = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
    const loc = r.location || r.geo || "";
    const dc = r.dc_zh || r.dc || "—";
    const pctBw = Math.min(100, Math.round((r.mbps || 0) / bw * 100));
    tr.innerHTML =
      '<td class="rank ' + rankCls + '">' + (i + 1) + '</td>' +
      '<td class="ip" title="点击复制 IP">' + esc(r.ip) + (loc ? '<span class="sub">' + esc(loc) + '</span>' : '') + '</td>' +
      '<td>' + esc(dc) + (r.dc && r.dc_zh ? '<span class="sub">CF 机房 ' + esc(r.dc) + '</span>' : '') + '</td>' +
      '<td class="lat">' + (r.latency ?? r.ping ?? 0) + ' <span class="unit">ms</span></td>' +
      '<td class="loss" style="color:' + (r.loss == null ? "var(--muted)" : (r.loss <= 0.1 ? "var(--green)" : r.loss <= 0.3 ? "var(--accent)" : "var(--red, #e05252)")) + '">' + (r.loss == null ? "—" : Math.round(r.loss * 100) + '<span class="unit">%</span>') + '</td>' +
      '<td class="mbps" style="color:' + ((r.mbps || 0) >= bw ? "var(--green)" : "var(--accent)") + '">' + (r.mbps || 0) + '<span class="unit">Mbps</span><span class="sub">' + pctBw + '% of 期望带宽</span></td>' +
      '<td><span class="badge ' + (r.port === 443 ? "tls" : "plain") + '">' + (r.port === 443 ? "TLS:443" : "HTTP:80") + '</span></td>' +
      '<td><button class="rowcopy" title="复制 IP">📋</button></td>';
    tr.querySelector(".ip").onclick = () => copyText(r.ip);
    tr.querySelector(".rowcopy").onclick = () => copyText(r.ip);
    body.appendChild(tr);
  });
}
function copyText(t) { navigator.clipboard.writeText(t).then(() => toast("已复制：" + t)); }

/* ── CSV 下载（后端 /api/export，单一事实来源） ── */
function currentSource() {
  return lastResultSource === "latest" ? "latest" : "history&history_id=" + lastResultSource;
}
function downloadBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ── 历史 ── */
async function loadHistory() {
  const h = await fetch("/api/history").then(r => r.json());
  const el = $("#histList");
  if (!h.length) { el.innerHTML = '<div class="empty">暂无历史记录</div>'; return; }
  el.innerHTML = "";
  for (const e of h) {
    const best = (e.results || [])[0];
    const div = document.createElement("div");
    div.className = "hist-item";
    div.innerHTML =
      '<div class="meta"><b>' + e.time + '</b>' +
      (e.ipVer === "v6" ? "IPv6" : "IPv4") + ' · ' + (e.tls ? "TLS" : "HTTP") + ' · ' + e.count + ' 个结果 · 用时 ' + (e.elapsed || 0) + 's' +
      (best ? ' · 最佳：' + best.ip + ' ' + best.mbps + 'Mbps（' + (best.dc_zh || best.dc || '—') + '）' : '') + '</div>' +
      '<div class="ops">' +
      '<button class="btn small act-reuse">重新使用参数</button>' +
      '<button class="btn small act-view">查看</button>' +
      '<button class="btn small danger act-del">删除</button></div>';
    div.querySelector(".act-del").onclick = async () => {
      await fetch("/api/history", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", id: e.id }) });
      loadHistory();
    };
    div.querySelector(".act-view").onclick = () => { lastResultSource = e.id; showResults(e); };
    div.querySelector(".act-reuse").onclick = () => {
      const p = e.params || {};
      state.ipVer = p.ipVer || state.ipVer;
      state.tls = p.tls === undefined ? state.tls : !!p.tls;
      state.count = p.count || state.count;
      setSeg("#segVer", state.ipVer);
      setSeg("#segTls", state.tls);
      if (p.count) setSeg("#segCount", String(p.count));
      if (typeof p.colo === "string") { state.colo = p.colo; $("#selDC").value = p.colo || ""; }
      syncCountryChipVisibility();
      if (Array.isArray(p.countries) && p.countries.length) setChips(p.countries);
      for (const [id, k] of [["#inSample", "sample"], ["#inSecs", "speedSecs"], ["#inMB", "speedMB"], ["#inBW", "bw"], ["#inMinSpeed", "minSpeed"]]) {
        if (p[k] != null) $(id).value = p[k];
      }
      toast("已载入历史参数");
      $("#settingsCard").scrollIntoView({ behavior: "smooth" });
    };
    el.appendChild(div);
  }
}
function setSeg(sel, val) {
  $$(sel + " button").forEach(b => b.classList.toggle("on", String(b.dataset.v) === String(val)));
}
function setChips(codes) {
  state.countries = new Set(codes);
  // chip 按 COUNTRIES 顺序生成（渲染时已去重），按同序匹配
  const uniq = COUNTRIES.filter((x, i) => COUNTRIES.findIndex(y => y[0] === x[0]) === i);
  $$("#countryChips .chip").forEach((c, i) => {
    c.classList.toggle("on", state.countries.has(uniq[i][0]));
  });
}

/* ── Toast ── */
let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

/* ── IP 池管理 ── */
async function openPools() {
  $("#poolsModal").style.display = "";
  await loadPools();
}
async function loadPools() {
  const list = await fetch("/api/pools").then(r => r.json()).catch(() => []);
  const el = $("#poolList");
  el.innerHTML = "";
  const total = list.reduce((s, p) => s + p.size, 0);
  $("#poolStats").textContent = list.length ? `${list.length} 个节点 · ${total} 个 IP` : "（空）";
  if (!list.length) { el.innerHTML = '<div class="empty">暂无 IP 池，完成扫描后自动生成</div>'; return; }
  for (const p of list) {
    const row = document.createElement("div");
    row.className = "pool-row";
    const ipsPreview = (p.ips || []).slice(0, 3).join(", ") + (p.ips && p.ips.length > 3 ? " …" : "");
    row.innerHTML =
      '<span class="code">' + esc(p.code) + '</span>' +
      '<span class="cc">' + esc((p.cc_zh || "") + (p.cc ? " (" + p.cc + ")" : "")) + '</span>' +
      '<span class="ips" title="' + esc((p.ips || []).join("\n")) + '">' + esc(ipsPreview) + '</span>' +
      '<span class="sz">' + p.size + ' IP</span>' +
      '<button class="rowcopy" title="复制全部 IP">📋</button>' +
      '<button class="rowcopy" title="清空此池">🗑️</button>';
    row.querySelector("[title='复制全部 IP']").onclick = () => copyText(p.ips.join("\n"));
    row.querySelector("[title='清空此池']").onclick = async () => {
      if (!confirm(`清空 ${p.code} 池（${p.size} 个 IP）？`)) return;
      await fetch("/api/pools", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "clear", code: p.code }) });
      toast(`${p.code} 池已清空`);
      loadPools(); loadColos();
    };
    el.appendChild(row);
  }
}
async function poolAdd() {
  const ips = $("#poolIps").value.trim();
  if (!ips) return toast("请填写 IP 列表");
  const btn = $("#btnPoolAdd");
  btn.disabled = true;
  const out = $("#poolProbeResult");
  out.textContent = "⏳ 正在探测各 IP 实际服务节点（cf-meta-colo）…";
  try {
    const d = await fetch("/api/pools", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", code: "", ips }) }).then(r => r.json());
    if (d.error) { out.textContent = "✘ " + d.error; return; }
    const lines = [];
    for (const it of d.details || []) {
      if (!it.ok) lines.push(`  ✘ ${it.ip}  ${it.reason}`);
    }
    const by = Object.entries(d.by_colo || {});
    if (by.length) {
      lines.push("");
      lines.push("✔ 入池成功：");
      for (const [colo, lst] of by) lines.push(`  → ${colo}：+${lst.length} 个（${lst.join(", ")}）`);
    }
    const summary = [
      `入池 ${d.added} 个`,
      `非 CF IP 拒绝 ${d.rejected}`,
      `节点不符拒绝 ${d.mismatch}`,
      `探测失败 ${d.failed}`,
    ].join(" · ");
    out.innerHTML = esc(summary + "\n" + lines.join("\n"));
    toast(`入池 ${d.added} · 拒绝 ${d.rejected + d.mismatch} · 失败 ${d.failed}`);
    $("#poolIps").value = "";
    loadPools(); loadColos();
  } finally {
    btn.disabled = false;
  }
}

/* ── 状态栏 / 系统信息 ── */
async function loadStatus() {
  const s = await fetch("/api/data-status").then(r => r.json()).catch(() => null);
  if (!s) return;
  const mb = b => b > 1048576 ? (b / 1048576).toFixed(1) + "MB" : Math.round(b / 1024) + "KB";
  $("#stVer").textContent = "FastCF v" + (s.version || "1.0");
  $("#stDir").textContent = s.data_dir;
  $("#stDir").title = s.data_dir;
  $("#stXdb").textContent = "xdb v4 " + mb(s.xdb_v4) + " · v6 " + mb(s.xdb_v6);
  $("#stPool").textContent = `池 ${s.pool_dc} 节点 / ${s.pool_ips} IP`;
  $("#stColo").textContent = `colo 表 ${s.colo_count} 节点 · Py ${s.python}`;
}
async function openInfo() {
  const s = await fetch("/api/data-status").then(r => r.json()).catch(() => ({}));
  const mb = b => b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  const rows = [
    ["版本", "FastCF " + (s.version || "1.0")],
    ["Python", s.python || "?"],
    ["数据目录", `<code>${esc(s.data_dir || "")}</code>`],
    ["ip2region v4", s.xdb_v4 ? mb(s.xdb_v4) : "未下载"],
    ["ip2region v6", s.xdb_v6 ? mb(s.xdb_v6) : "未下载"],
    ["CF IP 缓存", s.cf_cache ? mb(s.cf_cache) : "未缓存"],
    ["IP 池", `${s.pool_dc || 0} 节点 · ${s.pool_ips || 0} IP（7 天 TTL）`],
    ["colo 参考表", `${s.colo_count || 0} 个节点`],
    ["运行状态", s.running ? "⏳ 扫描中" : "空闲"],
    ["参考实现", '<a href="https://github.com/XIU2/CloudflareSpeedTest" target="_blank" style="color:var(--accent)">XIU2/CloudflareSpeedTest</a>'],
  ];
  $("#infoBody").innerHTML = '<table class="info-table">' +
    rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") + "</table>";
  $("#infoModal").style.display = "";
}

/* ── 启动 ── */
initTheme();
initControls();
(async () => {
  const s = await fetch("/api/status").then(r => r.json()).catch(() => ({}));
  if (s.result) { lastResultSource = "latest"; showResults(s.result); }
  loadHistory();
  loadColos();
  loadStatus();
  // 定期刷新 DC 池数量与状态栏
  setInterval(loadColos, 30000);
  setInterval(loadStatus, 30000);
})();
