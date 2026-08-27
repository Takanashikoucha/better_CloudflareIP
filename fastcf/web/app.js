/* FastCF 前端逻辑（v2：IPv4 · 443/TLS · Top5 · 指定 DC / 全局随机） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let lastResult = null;
let lastResultSource = "latest";  // "latest" | {id} — 当前展示结果对应的导出来源
let sse = null;
let logN = 0;
let resSortKey = "ping";
const state = {
  mode: "",        // "" | "DC" | "RANDOM"
  colo: "",        // 指定 DC 三字码（mode=DC 时有效）
  randomCount: 150,// 全局随机采样的 IP 数量
  speedSecs: 8,
  speedMB: 50,
  minSpeed: 0,     // 下载速度下限（Mbps）；0 = 任何速度 >0 都达标
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
  numBind("#inSecs", "speedSecs", 3, 60, 8);
  numBind("#inMB", "speedMB", 10, 1000, 50);
  numBind("#inMinSpeed", "minSpeed", 0, 10000, 0);
  numBind("#inRandCount", "randomCount", 10, 2000, 150);

  // 来源下拉：指定 DC / 全局随机
  $("#selDC").onchange = (e) => {
    const v = e.target.value;
    if (v === "RANDOM") { state.mode = "RANDOM"; state.colo = ""; }
    else if (v) { state.mode = "DC"; state.colo = v; }
    else { state.mode = ""; state.colo = ""; }
  };

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
  $("#btnClosePools").onclick = closePools;
  // 点击遮罩关闭面板（与按钮等价，需停掉自动刷新）
  $("#poolsModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) closePools(); });
  $("#btnInfo").onclick = openInfo;
  $("#btnCloseInfo").onclick = () => $("#infoModal").style.display = "none";
  $("#btnPoolAdd").onclick = poolAdd;
  $("#btnPoolInit").onclick = poolInit;
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

/* ── DC 节点选择：指定 DC（国家分组）/ 全局随机 ── */
async function loadColos() {
  const sel = $("#selDC");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">— 请选择 —</option>';
  const oRandom = document.createElement("option");
  oRandom.value = "RANDOM";
  oRandom.textContent = "🌐 全局随机（全 CF 官方 IPv4 段）";
  sel.appendChild(oRandom);
  try {
    const groups = await fetch("/api/colos").then(r => r.json());
    for (const g of groups) {
      const og = document.createElement("optgroup");
      og.label = `${g.cc_zh} (${g.cc})`;
      for (const c of g.items) {
        const o = document.createElement("option");
        o.value = c.code;
        o.textContent = c.name.replace(/^中国·?/, "") + (c.pool ? `  · 池 ${c.pool} IP` : "");
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
  } catch { /* 加载失败不影响使用 */ }
  // 恢复选择（含 RANDOM / 具体 DC）
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
  // 池统计实时刷新
  if (d.pool_ips != null) {
    $("#stPool").textContent = `池 ${d.pool_dc} 节点 / ${d.pool_ips} IP`;
  }
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
  return { prepare: "准备中", geo: "采样 IP", revalidate: "池重验", rtt: "ping 预筛", speed: "下载测速", done: "扫描完成", error: "出错" }[s] || s;
}
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ── 扫描 ── */
function params() {
  if (!state.mode) return null;
  return {
    mode: state.mode,
    colo: state.colo || "",
    randomCount: state.randomCount || 150,
    speedSecs: state.speedSecs || 8,
    speedMB: state.speedMB || 50,
    minSpeed: state.minSpeed || 0,
  };
}
async function startScan() {
  const p = params();
  if (!p) return toast("请先选择 IP 来源（指定 DC 或全局随机）");
  const ok = await fetch("/api/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
  const r = await ok.json();
  if (r.error) return toast(r.error);
  setRunningUI(true);
  openSSE();
}
function setRunningUI(on) {
  const btn = $("#btnScan");
  btn.disabled = on;
  btn.textContent = on ? "⏳ 扫描中..." : "🚀 开始优选";
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
  // 排序：ping 升序 / loss 升序 / mbps 降序（i 表示保持扫描排名）
  if (resSortKey === "mbps") rows.sort((a, b) => (b.mbps || 0) - (a.mbps || 0));
  else if (resSortKey === "loss") rows.sort((a, b) => (a.loss ?? 1) - (b.loss ?? 1));
  else if (resSortKey === "ping") rows.sort((a, b) => (a.latency ?? a.ping ?? 1e9) - (b.latency ?? b.ping ?? 1e9));
  else if (resSortKey === "dc") rows.sort((a, b) => String(a.dc || "").localeCompare(String(b.dc || "")));
  const body = $("#resBody");
  body.innerHTML = "";
  $("#resEmpty").style.display = rows.length ? "none" : "";
  rows.forEach((r, i) => {
    const tr = document.createElement("tr");
    const rankCls = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
    const loc = r.location || r.geo || "";
    const dc = r.dc_zh || r.dc || "—";
    const bwOk = state.minSpeed > 0 ? (r.mbps || 0) >= state.minSpeed : (r.mbps || 0) > 0;
    tr.innerHTML =
      '<td class="rank ' + rankCls + '">' + (i + 1) + '</td>' +
      '<td class="ip" title="点击复制 IP">' + esc(r.ip) + (loc ? '<span class="sub">' + esc(loc) + '</span>' : '') + '</td>' +
      '<td>' + esc(dc) + (r.dc && r.dc_zh ? '<span class="sub">CF 机房 ' + esc(r.dc) + '</span>' : '') + '</td>' +
      '<td class="lat">' + (r.latency ?? r.ping ?? 0) + ' <span class="unit">ms</span></td>' +
      '<td class="loss" style="color:' + (r.loss == null ? "var(--dim)" : (r.loss <= 0.1 ? "var(--green)" : r.loss <= 0.3 ? "var(--yellow)" : "var(--red)")) + '">' + (r.loss == null ? "—" : Math.round(r.loss * 100) + '<span class="unit">%</span>') + '</td>' +
      '<td class="mbps" style="color:' + (bwOk ? "var(--green)" : "var(--accent)") + '">' + (r.mbps || 0) + '<span class="unit">Mbps</span>' + (state.minSpeed > 0 ? '<span class="sub">' + (bwOk ? '≥ 下限 ' + state.minSpeed : '未达下限') : '') + '</td>' +
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
    const p = e.params || {};
    const modeName = e.mode === "DC" ? "指定 DC " + (e.colo || "") :
                     e.mode === "DC+随机" ? "指定 DC " + (e.colo || "") + "+随机" : "全局随机";
    const div = document.createElement("div");
    div.className = "hist-item";
    div.innerHTML =
      '<div class="meta"><b>' + e.time + '</b>' +
      modeName + ' · Top' + e.count + ' · 用时 ' + (e.elapsed || 0) + 's' +
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
      // 兼容旧历史（v1 参数）：countries/colo 非 RANDOM → 指定 DC；RANDOM → 全局随机
      if (p.colo === "RANDOM") { state.mode = "RANDOM"; state.colo = ""; }
      else if (p.colo) { state.mode = "DC"; state.colo = p.colo; }
      else if (p.mode === "RANDOM") { state.mode = "RANDOM"; state.colo = ""; }
      else if (p.mode === "DC" && p.colo) { state.mode = "DC"; state.colo = p.colo; }
      else { state.mode = ""; state.colo = ""; }
      $("#selDC").value = state.mode === "RANDOM" ? "RANDOM" : (state.colo || "");
      state.randomCount = p.randomCount || state.randomCount;
      $("#inRandCount").value = state.randomCount;
      state.speedSecs = p.speedSecs || state.speedSecs;
      state.speedMB = p.speedMB || state.speedMB;
      state.minSpeed = p.minSpeed || 0;
      $("#inSecs").value = state.speedSecs;
      $("#inMB").value = state.speedMB;
      $("#inMinSpeed").value = state.minSpeed;
      toast(state.mode ? "已载入历史参数" : "已载入历史参数（来源需重新选择）");
      $("#settingsCard").scrollIntoView({ behavior: "smooth" });
    };
    el.appendChild(div);
  }
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
let poolsTimer = null;  // 面板打开期间的自动刷新
function openPools() {
  $("#poolsModal").style.display = "";
  loadPools();
  clearInterval(poolsTimer);
  poolsTimer = setInterval(loadPools, 10000);
}
function closePools() {
  $("#poolsModal").style.display = "none";
  clearInterval(poolsTimer);
  poolsTimer = null;
}
async function loadPools() {
  if ($("#poolsModal").style.display !== "") return;  // 面板未打开时跳过（定时器可能仍在跑）
  const list = await fetch("/api/pools").then(r => r.json()).catch(() => []);
  const el = $("#poolList");
  el.innerHTML = "";
  const total = list.reduce((s, p) => s + p.size, 0);
  $("#poolStats").textContent = list.length ? `${list.length} 个节点 · ${total} 个 IP` : "（空）";
  if (!list.length) { el.innerHTML = '<div class="empty">暂无 IP 池，手动添加或完成扫描后自动生成</div>'; return; }
  for (const p of list) {
    const row = document.createElement("div");
    row.className = "pool-row";
    const ipsPreview = (p.ips || []).slice(0, 3).join(", ") + (p.ips && p.ips.length > 3 ? " …" : "");
    row.innerHTML =
      '<span class="code">' + esc(p.code) + (p.expired ? ' <span class="sub" style="display:inline">⏳</span>' : '') + '</span>' +
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
async function poolInit() {
  const btn = $("#btnPoolInit");
  const out = $("#poolProbeResult");
  const refresh = $("#chkRefreshCache").checked;
  if (!confirm(`对每个 CF IPv4 段的首个 IP 并发探测实际服务节点并入池（约 877 个 IP，并发 20，预计数分钟）。\n${refresh ? "将先强制刷新 CF 段缓存（绕过 7 天 TTL）。\n" : ""}继续？`)) return;
  btn.disabled = true;
  out.textContent = "⏳ 正在段首 IP 探测（cf-meta-colo）…" + (refresh ? "（先刷新段缓存）" : "");
  try {
    const d = await fetch("/api/pools", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "init", refresh_cache: refresh }) }).then(r => r.json());
    if (d.error) { out.textContent = "✘ " + d.error; return; }
    const lines = [];
    const by = Object.entries(d.by_colo || {});
    if (by.length) {
      lines.push("✔ 入池成功：");
      by.sort((a, b) => b[1].length - a[1].length).slice(0, 15).forEach(([colo, lst]) =>
        lines.push(`  → ${colo}：+${lst.length} 个（${lst.slice(0, 3).join(", ")}${lst.length > 3 ? " …" : ""}）`));
      if (by.length > 15) lines.push(`  … 其余 ${by.length - 15} 个节点见池列表`);
    }
    if ((d.failed || 0) + (d.mismatch || 0) > 0) {
      lines.push("");
      lines.push(`探测失败 ${d.failed || 0} 个（不可达/无响应）· 节点不符拒绝 ${d.mismatch || 0} 个`);
    }
    const summary = `探测 ${d.total || "?"} 个段首 IP · 入池 ${d.added} · 失败 ${d.failed || 0}`;
    out.innerHTML = esc(summary + (lines.length ? "\n" + lines.join("\n") : ""));
    toast(`段首 IP 探测：入池 ${d.added} / ${d.total || "?"}`);
    loadPools(); loadColos();
  } finally {
    btn.disabled = false;
  }
}
async function poolAdd() {
  const ips = $("#poolIps").value.trim();
  if (!ips) return toast("请填写 IPv4 列表");
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
      `非 CF IPv4 拒绝 ${d.rejected}`,
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
  $("#stVer").textContent = "FastCF v" + (s.version || "2.0");
  $("#stDir").textContent = s.data_dir;
  $("#stDir").title = s.data_dir;
  $("#stXdb").textContent = `CF 段 ${s.cf_cidrs || "?"} 条` + (s.cf_cache ? " · " + mb(s.cf_cache) : "");
  $("#stPool").textContent = `池 ${s.pool_dc} 节点 / ${s.pool_ips} IP` + (s.pool_expired ? "（待重验）" : "");
  $("#stColo").textContent = `colo 表 ${s.colo_count} 节点 · Py ${s.python}`;
}
async function openInfo() {
  const s = await fetch("/api/data-status").then(r => r.json()).catch(() => ({}));
  const mb = b => b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  const rows = [
    ["版本", "FastCF " + (s.version || "2.0")],
    ["Python", s.python || "?"],
    ["数据目录", `<code>${esc(s.data_dir || "")}</code>`],
    ["CF IPv4 段缓存", `${s.cf_cidrs || "?"} 条 CIDR（TYOYO1/CF-ASN 全量段） · ${s.cf_cache ? mb(s.cf_cache) : "未缓存"}`],
    ["IP 池", `${s.pool_dc || 0} 节点 · ${s.pool_ips || 0} IP（7 天 TTL，指定 DC 扫描时事件性重验）`],
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
