/* FastCF 前端逻辑（v3：双源合并采样 · 深色玻璃拟态 UI） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  mode: "DC",          // "DC" | "RANDOM"
  colo: "",            // 指定 DC 三字码
  randomCount: 150,    // 全局随机采样的 IP 数量
  speedSecs: 8,
  speedMB: 50,
  minSpeed: 0,         // 速度下限（Mbps）；0 = 任何速度 >0 都达标
};

let lastResult = null;
let lastResultSource = "latest";   // "latest" | {id}
let sse = null;
let logN = 0;
let resSortKey = "ping";
let resSortAsc = true;
let coloGroups = [];               // /api/colos 缓存
let dataStatus = null;

/* ═══ 工具 ═══ */

function api(path, opts) {
  opts = opts || {};
  return fetch(path, opts).then(async (r) => {
    let d = null;
    try { d = await r.json(); } catch (e) { d = {}; }
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  });
}

let toastTimer = null;
function toast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtAgo(ts) {
  if (!ts) return "从未";
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 3600) return Math.max(1, Math.floor(d / 60)) + " 分钟前";
  if (d < 86400) return Math.floor(d / 3600) + " 小时前";
  return Math.floor(d / 86400) + " 天前";
}

/* ═══ 主题 ═══ */

function initTheme() {
  const saved = localStorage.getItem("fastcf-theme") || "dark";
  setTheme(saved, true);
}
function setTheme(t, silent) {
  document.body.dataset.theme = t;
  localStorage.setItem("fastcf-theme", t);
  if (!silent) toast(t === "dark" ? "已切换深色模式" : "已切换浅色模式");
}

/* ═══ 滑杆 ═══ */

function paintRange(el) {
  const min = +el.min || 0, max = +el.max || 100;
  el.style.setProperty("--fill", ((+el.value - min) / (max - min) * 100) + "%");
}

function bindRange(id, key, valId, clamp) {
  const el = $(id);
  const sync = () => {
    let v = +el.value;
    if (clamp) v = Math.max(clamp[0], Math.min(clamp[1], v));
    state[key] = v;
    if (valId) $(valId).textContent = v;
    paintRange(el);
  };
  el.addEventListener("input", sync);
  el.value = state[key];
  sync();
}

/* ═══ 模式分段控件 ═══ */

function setMode(m) {
  state.mode = m;
  $$(".seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  $("#fDC").hidden = m !== "DC";
  $("#fRand").hidden = m !== "RANDOM";
}

/* ═══ DC 下拉（国家分组 + 搜索）═══ */

function fillDCSelect(filter) {
  const f = (filter || "").trim().toLowerCase();
  const mk = (sel, withPool) => {
    sel.innerHTML = "";
    const head = document.createElement("option");
    head.value = "";
    head.textContent = f ? "无匹配节点" : (withPool ? "— 按实际 colo —" : "— 请选择节点 —");
    sel.appendChild(head);
    for (const g of coloGroups) {
      const items = g.items.filter((it) => !f ||
        it.code.toLowerCase().includes(f) || (it.name || "").toLowerCase().includes(f));
      if (!items.length) continue;
      const og = document.createElement("optgroup");
      og.label = (g.cc_zh || g.cc) + "（" + items.length + "）";
      for (const it of items) {
        const o = document.createElement("option");
        o.value = it.code;
        o.textContent = withPool ? `${it.code} · ${it.name}${it.pool ? "（池 " + it.pool + "）" : ""}`
                                 : `${it.code} · ${it.name}`;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
  };
  mk($("#selDC"), true);
  mk($("#poolDC"), false);
  if ($("#selDC").value !== state.colo && state.colo) $("#selDC").value = state.colo;
}

async function refreshColos() {
  try {
    coloGroups = await api("/api/colos");
    fillDCSelect($("#dcSearch").value);
  } catch (e) { /* 静默：状态栏提示即可 */ }
}

/* ═══ 数据状态栏 ═══ */

async function refreshDataStatus() {
  try {
    dataStatus = await api("/api/data-status");
    $("#stCidr").textContent = dataStatus.cf_cidrs + " 段";
    $("#stExt").textContent = dataStatus.ext_ips > 0 ? dataStatus.ext_ips.toLocaleString() + " 条" : "—";
    $("#stPool").textContent = dataStatus.pool_ips + " / " + dataStatus.pool_dc;
    $("#stColo").textContent = dataStatus.colo_count + " 个";
    $("#stDir").textContent = dataStatus.data_dir;
    $("#stVer").textContent = "v" + dataStatus.version + " · Py" + dataStatus.python +
      " · 清单 " + fmtAgo(dataStatus.ext_ts);
    if (!$("#poolsModal").hidden) renderPoolsStats();
  } catch (e) { /* 忽略瞬时错误 */ }
}

/* ═══ 状态指示 ═══ */

function setRunning(running, stage) {
  const ind = $("#runInd");
  ind.classList.toggle("busy", running);
  ind.classList.toggle("done", !running && stage === "done");
  $("#runTxt").textContent = running ? "扫描中" : (stage === "done" ? "完成" : "空闲");
  $("#btnScan").disabled = running;
  $("#btnCancel").hidden = !running;
}

/* ═══ 进度与日志 ═══ */

function renderProgress(s) {
  const pct = s.pct == null ? 0 : s.pct;
  $("#stageFill").style.width = pct + "%";
  $("#stagePct").textContent = pct + "%";
  const names = {
    prepare: "准备", revalidate: "池重验", geo: "随机采样",
    rtt: "ICMP 预筛", speed: "下载测速", done: "完成", error: "出错",
  };
  $("#stageName").textContent = names[s.stage] || (s.stage || "—");
  $("#stageDetail").textContent = s.detail || "";
  if (s.elapsed != null) $("#stageElapsed").textContent = "耗时 " + s.elapsed + "s";
  renderLogs(s.logs);
}

function renderLogs(logs) {
  if (!logs) return;
  const box = $("#logBox");
  // 仅追加新增行，避免全量重绘
  if (logs.length > logN) {
    const frag = document.createDocumentFragment();
    for (let i = logN; i < logs.length; i++) {
      const L = logs[i];
      const div = document.createElement("div");
      div.className = "logline" + (L.level && L.level !== "info" ? " " + L.level : "");
      div.innerHTML = `<span class="ts">${esc(L.ts)}</span>${esc(L.msg)}`;
      frag.appendChild(div);
    }
    logN = logs.length;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.appendChild(frag);
    if (nearBottom || logs.length <= 30) box.scrollTop = box.scrollHeight;
  } else if (logs.length < logN) {
    // 新扫描开始（日志重置）
    logN = 0;
    box.innerHTML = "";
    renderLogs(logs);
  }
}

/* ═══ SSE ═══ */

function initSSE() {
  sse = new EventSource("/api/stream");
  sse.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.type === "none") return;
    setRunning(!!d.running, d.stage);
    renderProgress(d);
    if (d.pool_ips != null) {
      $("#stPool").textContent = d.pool_ips + " / " + d.pool_dc;
    }
    if (!d.running && d.stage === "done") loadLatest();
  };
  sse.onerror = () => { /* EventSource 自动重连 */ };
}

/* ═══ 扫描 ═══ */

function params() {
  return {
    mode: state.mode,
    colo: state.mode === "DC" ? state.colo : "",
    randomCount: state.randomCount,
    speedSecs: state.speedSecs,
    speedMB: state.speedMB,
    minSpeed: state.minSpeed,
  };
}

async function startScan() {
  if (state.mode === "DC" && !state.colo) {
    toast("请先选择一个节点", "err");
    $("#dcSearch").focus();
    return;
  }
  try {
    await api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params()),
    });
    logN = 0;
    $("#logBox").innerHTML = "";
    toast("扫描已开始", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function cancelScan() {
  if (!confirm("确定取消当前扫描？\n已测出的部分结果将保留在日志中。")) return;
  try {
    await api("/api/cancel", { method: "POST" });
    toast("取消请求已发送", "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ═══ 结果表 ═══ */

function loadLatest() {
  api("/api/status").then((d) => {
    if (d.result) {
      lastResult = d.result;
      lastResultSource = "latest";
      renderResults();
      switchTab("results");
    }
  }).catch(() => {});
  refreshHistory();
}

function renderResults() {
  const res = (lastResult && lastResult.results) || [];
  const body = $("#resBody");
  const empty = $("#resEmpty");
  if (!res.length) {
    body.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const maxPing = Math.max(1, ...res.map((r) => r.ping || 0));
  const sorted = res.map((r, i) => ({ ...r, i }));
  sorted.sort((a, b) => {
    let va = a[resSortKey], vb = b[resSortKey];
    if (resSortKey === "ip") { va = va || ""; vb = vb || "";
      return resSortAsc ? va.localeCompare(vb) : -va.localeCompare(vb); }
    va = va == null ? (resSortAsc ? 1e9 : -1) : va;
    vb = vb == null ? (resSortAsc ? 1e9 : -1) : vb;
    return resSortAsc ? va - vb : vb - va;
  });
  body.innerHTML = sorted.map((r) => {
    const pingW = Math.max(4, Math.round(100 - (r.ping / maxPing) * 90));
    const loss = r.loss == null ? 0 : r.loss;
    const lossCls = loss === 0 ? "loss-good" : loss < 0.25 ? "loss-mid" : "loss-bad";
    const sp = r.mbps;
    const spCls = sp > 0 ? (sp >= 100 ? "speed-high" : sp >= 30 ? "speed-mid" : "speed-low") : "speed-zero";
    return `<tr>
      <td class="mono" style="color:var(--text-faint)">${r.i + 1}</td>
      <td class="ip">${esc(r.ip)}${r.cfRay ? `<span class="sub">${esc(r.cfRay)}</span>` : ""}</td>
      <td>${r.dc ? `<span class="dc-badge">${esc(r.dc)}</span>` : ""}<span>${esc(r.dc_zh || "—")}</span></td>
      <td><div class="ping-cell"><div class="ping-bar"><i style="width:${pingW}%"></i></div>
          <span class="mono">${r.ping ? r.ping + " ms" : "—"}</span></div></td>
      <td class="mono ${lossCls}">${(loss * 100).toFixed(0)}%</td>
      <td class="mono ${spCls}">${sp ? sp + " Mbps" : "—"}</td>
      <td></td>
    </tr>`;
  }).join("");
}

/* ═══ 历史 ═══ */

async function refreshHistory() {
  try {
    const h = await api("/api/history");
    const list = $("#histList");
    $("#histCount").textContent = h.length ? h.length + " 条记录" : "";
    if (!h.length) {
      list.innerHTML = '<div class="empty" id="histEmpty">暂无历史记录</div>';
      return;
    }
    list.innerHTML = h.map((e) => {
      const p = e.params || {};
      const modeTxt = p.mode === "DC" ? `指定 ${esc(p.colo || "?")}` : "全局随机";
      const n = (e.results || []).length;
      return `<div class="hist-item" data-id="${e.id}">
        <div class="hist-main">
          <div class="hist-time">${esc(e.time)}</div>
          <div class="hist-params">${modeTxt} · ${esc(e.mode || "")} · ${n} 个结果 · 用时 ${e.elapsed ?? "—"}s
            ${e.minSpeed > 0 ? " · 下限 " + e.minSpeed + "Mbps" : ""}</div>
        </div>
        <div class="hist-badges">
          <span class="hist-badge">v${esc(e.ipVer || "v4")}</span>
          ${e.colo ? `<span class="hist-badge">${esc(e.colo)}</span>` : ""}
        </div>
        <div class="hist-ops">
          <button class="btn-ghost" data-act="view">查看</button>
          <button class="btn-ghost" data-act="reuse">复用参数</button>
          <button class="btn-ghost" data-act="csv">CSV</button>
          <button class="btn-ghost danger" data-act="del">删除</button>
        </div>
      </div>`;
    }).join("");
    $$("#histList .hist-item").forEach((el) => {
      const id = +el.dataset.id;
      const entry = h.find((x) => x.id === id);
      el.querySelectorAll("[data-act]").forEach((btn) => {
        btn.onclick = async () => {
          const act = btn.dataset.act;
          try {
            if (act === "view") {
              lastResult = entry;
              lastResultSource = { id };
              renderResults();
              switchTab("results");
            } else if (act === "reuse") {
              applyParams(entry.params || {});
              toast("已复用历史参数", "ok");
            } else if (act === "csv") {
              download(`/api/export?fmt=csv&source=history&history_id=${id}`);
            } else if (act === "del") {
              await api("/api/history", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "delete", id }),
              });
              refreshHistory();
            }
          } catch (e) { toast(e.message, "err"); }
        };
      });
    });
  } catch (e) { /* 忽略 */ }
}

function applyParams(p) {
  if (p.mode === "RANDOM") setMode("RANDOM");
  else setMode("DC");
  state.colo = (p.colo || "").toUpperCase();
  $("#selDC").value = state.colo;
  state.randomCount = p.randomCount || 150;
  state.speedSecs = p.speedSecs || 8;
  state.speedMB = p.speedMB || 50;
  state.minSpeed = p.minSpeed || 0;
  $("#inRandCount").value = state.randomCount;
  $("#valRand").textContent = state.randomCount;
  paintRange($("#inRandCount"));
  $("#inSecs").value = state.speedSecs;
  $("#valSecs").textContent = state.speedSecs;
  paintRange($("#inSecs"));
  $("#inMB").value = state.speedMB;
  $("#valMB").textContent = state.speedMB;
  paintRange($("#inMB"));
  $("#inMinSpeed").value = state.minSpeed;
}

/* ═══ Tabs ═══ */

function switchTab(name) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("show", t.id === "tab-" + name));
}

/* ═══ 导出 ═══ */

function download(url) {
  const a = document.createElement("a");
  a.href = url;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ═══ IP 池管理 ═══ */

function showPools() {
  $("#poolsModal").hidden = false;
  refreshColos().then(loadPools);
}
function hidePools() { $("#poolsModal").hidden = true; }

async function loadPools() {
  try {
    const d = await api("/api/pools");
    const list = $("#poolList");
    if (!d.length) {
      list.innerHTML = '<div class="empty" style="padding:26px">池为空 — 手动添加或执行段首 IP 探测初始化</div>';
      renderPoolsStats();
      return;
    }
    list.innerHTML = d.map((p) => `
      <div class="pool-row" data-code="${esc(p.code)}">
        <div class="pool-row-head">
          <span class="pool-code">${esc(p.code)}</span>
          <span class="pool-name">${esc(p.cc_zh || p.cc || "")} ${p.expired ? '<span class="expired-tag">· 已过期</span>' : ""}</span>
          <span class="pool-meta">
            <span>${p.size} IP</span>
            <button class="btn-ghost danger" data-act="clear">清空</button>
          </span>
        </div>
        <div class="pool-ips">${p.ips.map(esc).join("  ·  ")}</div>
      </div>`).join("");
    list.querySelectorAll("[data-act='clear']").forEach((btn) => {
      btn.onclick = async () => {
        const code = btn.closest(".pool-row").dataset.code;
        if (!confirm(`清空 ${code} 节点池？`)) return;
        try {
          await api("/api/pools", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "clear", code }),
          });
          toast(`已清空 ${code}`, "ok");
          loadPools(); refreshDataStatus(); refreshColos();
        } catch (e) { toast(e.message, "err"); }
      };
    });
    renderPoolsStats();
  } catch (e) { toast(e.message, "err"); }
}

function renderPoolsStats() {
  if (dataStatus) {
    $("#poolStats").textContent =
      `池：${dataStatus.pool_dc} 节点 · ${dataStatus.pool_ips} IP` +
      (dataStatus.pool_expired ? " · 整体已过期" : "");
  }
}

async function poolAdd() {
  const ips = $("#poolIps").value;
  const code = $("#poolDC").value;
  if (!ips.trim()) { toast("请输入 IP 列表", "err"); return; }
  const btn = $("#btnPoolAdd");
  btn.disabled = true;
  const res = $("#poolProbeResult");
  res.hidden = false;
  res.textContent = "探测中…（并发拨号读取实际服务节点）";
  try {
    const d = await api("/api/pools", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", ips, code }),
    });
    const lines = [`入池 ${d.added} · 拒绝 ${d.rejected} · 不匹配 ${d.mismatch} · 失败 ${d.failed}`];
    (d.details || []).slice(0, 40).forEach((x) =>
      lines.push(`  ${x.ip} → ${x.ok ? "✔ 入池" : "✘ " + x.reason}`));
    res.textContent = lines.join("\n");
    toast(`已入池 ${d.added} 个 IP`, d.added ? "ok" : "err");
  } catch (e) {
    res.textContent = "失败：" + e.message;
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    loadPools(); refreshDataStatus(); refreshColos();
  }
}

async function poolInit() {
  const refresh = $("#chkRefreshCache").checked;
  const btn = $("#btnPoolInit");
  btn.disabled = true;
  const res = $("#poolProbeResult");
  res.hidden = false;
  res.textContent = "段首 IP 探测初始化中…（对官方段每段首个 IP 并发探测实际节点）";
  try {
    const d = await api("/api/pools", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "init", refresh_cache: refresh }),
    });
    res.textContent =
      `共探测 ${d.total} 个段首 IP：入池 ${d.added} · 失败 ${d.failed} · 不匹配 ${d.mismatch}`;
    toast("初始化完成", "ok");
  } catch (e) {
    res.textContent = "失败：" + e.message;
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    loadPools(); refreshDataStatus(); refreshColos();
  }
}

async function poolClearAll() {
  if (!confirm("清空全部 IP 池？此操作不可恢复。")) return;
  try {
    const d = await api("/api/pools", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "clear_all" }),
    });
    toast(`已清空 ${d.removed} 个 IP`, "ok");
    loadPools(); refreshDataStatus();
  } catch (e) { toast(e.message, "err"); }
}

/* ═══ 系统信息 ═══ */

function showInfo() {
  refreshDataStatus().then(() => {
    const d = dataStatus || {};
    const rows = [
      ["版本", "v" + (d.version || "?")],
      ["Python", d.python || "?"],
      ["数据目录", d.data_dir || "?"],
      ["官方 CF 段", `${d.cf_cidrs ?? "—"} 条 · 缓存 ${fmtAgo(d.cf_ts)}`],
      ["外部 IP 清单", `${(d.ext_ips ?? 0).toLocaleString()} 条（443 端口）· 缓存 ${fmtAgo(d.ext_ts)}`],
      ["已知节点", d.colo_count ?? "—"],
      ["IP 池", `${d.pool_ips ?? 0} IP / ${d.pool_dc ?? 0} 节点${d.pool_expired ? "（整体已过期）" : ""}`],
      ["清单源", d.ext_source || ""],
    ];
    $("#infoBody").innerHTML =
      '<div class="info-kv">' + rows.filter((r) => r[1]).map((r) =>
        `<div class="kv"><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`).join("") + "</div>";
    $("#infoModal").hidden = false;
  });
}
function hideInfo() { $("#infoModal").hidden = true; }

/* ═══ 控件绑定 ═══ */

function initControls() {
  bindRange("#inRandCount", "randomCount", "#valRand", [10, 2000]);
  bindRange("#inSecs", "speedSecs", "#valSecs", [3, 60]);
  bindRange("#inMB", "speedMB", "#valMB", [10, 1000]);

  $("#inMinSpeed").addEventListener("change", () => {
    state.minSpeed = Math.max(0, Math.min(10000, +$("#inMinSpeed").value || 0));
  });

  $$(".seg-btn").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
  $("#dcSearch").addEventListener("input", (e) => fillDCSelect(e.target.value));
  $("#selDC").addEventListener("change", (e) => { state.colo = e.target.value; });

  $("#btnScan").onclick = startScan;
  $("#btnCancel").onclick = cancelScan;
  $("#btnTheme").onclick = () =>
    setTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
  $("#btnPools").onclick = showPools;
  $("#btnClosePools").onclick = hidePools;
  $("#btnInfo").onclick = showInfo;
  $("#btnCloseInfo").onclick = hideInfo;
  $("#btnPoolAdd").onclick = poolAdd;
  $("#btnPoolInit").onclick = poolInit;
  $("#btnPoolClearAll").onclick = poolClearAll;
  $("#poolsModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) hidePools(); });
  $("#infoModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) hideInfo(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { hidePools(); hideInfo(); }
  });

  // Tabs
  $$(".tab-btn").forEach((b) => { b.onclick = () => switchTab(b.dataset.tab); });

  // 排序
  $("#resSort").addEventListener("change", (e) => {
    resSortKey = e.target.value;
    resSortAsc = true;
    renderResults();
  });
  $$("#resTable th.sortable").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.k;
      if (resSortKey === k) resSortAsc = !resSortAsc;
      else { resSortKey = k; resSortAsc = true; }
      $$("#resTable th").forEach((x) => x.classList.remove("on"));
      th.classList.add("on");
      $("#resSort").value = resSortKey === "i" ? "ping" : resSortKey;
      renderResults();
    };
  });

  // 导出
  $("#btnDlCsv").onclick = () => {
    if (!lastResult || !lastResult.results || !lastResult.results.length) {
      toast("没有可导出的结果", "err");
      return;
    }
    const q = lastResultSource === "latest" ? "source=latest"
                                            : `source=history&history_id=${lastResultSource.id}`;
    download(`/api/export?fmt=csv&${q}`);
  };
  $("#btnClearHist").onclick = async () => {
    if (!confirm("清空全部历史记录？")) return;
    try {
      await api("/api/history", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "clear" }),
      });
      toast("历史已清空", "ok");
      refreshHistory();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ═══ 启动 ═══ */

function init() {
  initTheme();
  initControls();
  setMode(state.mode);
  refreshColos();
  refreshDataStatus();
  refreshHistory();
  loadLatest();
  initSSE();
  // 数据状态低频刷新（扫描进行中由 SSE 携带池统计，无需轮询）
  setInterval(() => {
    if (!$("#runInd").classList.contains("busy")) refreshDataStatus();
  }, 60000);
}

document.addEventListener("DOMContentLoaded", init);
