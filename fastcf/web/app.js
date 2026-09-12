/* FastCF 前端逻辑（v5.0：深石墨蓝控制台 · SSE 增量日志流 · 状态单一来源 = 后端 AppState） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* ═══ 前端状态：仅保留"表单草稿"（运行态/结果/历史/池全部来自后端）═══ */
const state = {
  mode: "DC",
  colo: "",
  randomCount: 150,
  speedSecs: 8,
  speedMB: 50,
  minSpeed: 0,
};

let lastResult = null;
let lastResultSource = "latest";   // "latest" | 历史 id
let sse = null;
let localLogs = [];               // 本地日志数组（增量渲染；全量首帧时重置）
let resSortKey = "ping";
let resSortAsc = true;
let coloGroups = [];
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

function fmtElapsed(s) {
  if (s == null) return "";
  const m = Math.floor(s / 60), r = s % 60;
  return m ? m + "m " + r + "s" : r + "s";
}

function stageName(stage) {
  const map = {
    prepare: "准备", revalidate: "重验池", geo: "采样", rtt: "ping 预筛",
    speed: "下载测速", done: "完成", error: "错误",
  };
  return map[stage] || stage || "…";
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
    head.textContent = f ? "无匹配节点" : (withPool ? "— 请选择节点 —" : "— 按实际 colo —");
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

/* ═══ 数据状态条 ═══ */

function refreshDataStatus() {
  api("/api/data-status").then((d) => {
    dataStatus = d;
    $("#stCidr").textContent = d.cf_cidrs || "—";
    $("#stCidrSub").textContent = d.cf_ts ? "更新 " + fmtAgo(d.cf_ts) : "尚未获取";
    $("#stExt").textContent = d.ext_ips ? d.ext_ips.toLocaleString() : "—";
    $("#stExtSub").textContent = d.ext_ts ? "更新 " + fmtAgo(d.ext_ts) : "尚未获取";
    $("#stPool").textContent = d.pool_ips ? `${d.pool_dc} / ${d.pool_ips}` : "—";
    $("#stPoolSub").textContent = d.pool_expired ? "池已过期（事件性重验）" : "节点 / IP 数";
    $("#stColo").textContent = d.colo_count || "—";
    $("#stVer").textContent = `v${d.version} · Python ${d.python}`;
  }).catch(() => {});
}

/* ═══ 运行指示器 ═══ */

function setRunning(running, stage) {
  const ind = $("#runInd"), txt = $("#runTxt");
  ind.classList.toggle("busy", running);
  ind.classList.toggle("done", !running && stage === "done");
  txt.textContent = running ? (stageName(stage) + "…") : (stage === "error" ? "出错" : "空闲");
  $("#btnScan").disabled = running;
  $("#btnScanTxt").textContent = running ? "扫描中…" : "开始优选";
  $("#btnCancel").hidden = !running;
}

/* ═══ SSE 实时流 ═══ */

function openSSE() {
  if (sse) sse.close();
  sse = new EventSource("/api/stream");
  sse.onmessage = (e) => {
    let d;
    try { d = JSON.parse(e.data); } catch (err) { return; }
    if (d.type === "none") return;
    if (d.pool_dc != null) {
      $("#stPool").textContent = `${d.pool_dc} / ${d.pool_ips}`;
    }
    if (d.running) {
      setRunning(true, d.stage);
      $("#stageName").textContent = stageName(d.stage);
      $("#stagePct").textContent = (d.pct || 0) + "%";
      $("#stageFill").style.width = (d.pct || 0) + "%";
      $("#stageDetail").textContent = d.detail || "";
      $("#stageElapsed").textContent = fmtElapsed(d.elapsed);
    }
    // 日志渲染：增量（logDelta）；全量首帧（logDelta 空 + logs 非空）→ 重置本地数组
    if (d.logs && d.logs.length && !(d.logDelta && d.logDelta.length)) {
      localLogs = d.logs.slice();
      renderLogsFull();
    } else if (d.logDelta && d.logDelta.length) {
      localLogs.push(...d.logDelta);
      renderLogsDelta(d.logDelta);
    }
    if (!d.running && (d.stage === "done" || d.stage === "error")) {
      setRunning(false, d.stage);
      if (d.stage === "done") {
        $("#stageName").textContent = "完成";
        $("#stagePct").textContent = "100%";
        $("#stageFill").style.width = "100%";
        loadLatest();
        refreshDataStatus();
        refreshPools();
        refreshHistory();
      }
    }
  };
  sse.onerror = () => { /* EventSource 自动重连 */ };
}

/* ═══ 日志渲染（增量 / 全量）═══ */

function logLineEl(l) {
  const div = document.createElement("div");
  div.className = "logline" + (l.level && l.level !== "info" ? " " + l.level : "");
  div.innerHTML = `<span class="ts">${esc(l.ts)}</span>${esc(l.msg)}`;
  return div;
}

function renderLogsDelta(delta) {
  const box = $("#logBox");
  for (const l of delta) box.appendChild(logLineEl(l));
  box.scrollTop = box.scrollHeight;
}

function renderLogsFull() {
  const box = $("#logBox");
  box.innerHTML = "";
  for (const l of localLogs) box.appendChild(logLineEl(l));
  box.scrollTop = box.scrollHeight;
}

/* ═══ 结果表 ═══ */

function renderResults() {
  const body = $("#resBody");
  const empty = $("#resEmpty");
  const rows = (lastResult && lastResult.results) || [];
  body.innerHTML = "";
  empty.hidden = rows.length > 0;
  if (!rows.length) return;

  const maxPing = Math.max(...rows.map((r) => r.ping || 0), 1);
  const sorted = rows.slice().sort((a, b) => {
    const va = a[resSortKey] ?? 0, vb = b[resSortKey] ?? 0;
    const d = (typeof va === "string") ? va.localeCompare(vb) : va - vb;
    return resSortAsc ? d : -d;
  });

  for (const r of sorted) {
    const tr = document.createElement("tr");
    const lossCls = r.loss >= 0.5 ? "loss-bad" : r.loss >= 0.2 ? "loss-mid" : "loss-good";
    const speedCls = r.mbps >= 100 ? "speed-high" : r.mbps >= 50 ? "speed-mid" : r.mbps > 0 ? "speed-low" : "speed-zero";
    const rank = r.rank || (rows.indexOf(r) + 1);
    tr.innerHTML = `
      <td class="w-n"><span class="rank${rank === 1 ? " top" : ""}">${rank}</span></td>
      <td class="ip">${esc(r.ip)}<span class="sub mono">${esc(r.cfRay || "")}</span></td>
      <td><span class="dc-badge">${esc(r.dc || "—")}</span><span class="sub">${esc(r.location || r.dc_zh || "")}</span></td>
      <td><div class="ping-cell"><div class="ping-bar"><i style="width:${Math.min(100, (r.ping || 0) / maxPing * 100)}%"></i></div><b class="mono">${r.ping || 0}ms</b></div></td>
      <td class="mono ${lossCls}">${((r.loss || 0) * 100).toFixed(0)}%</td>
      <td class="mono ${speedCls}">${r.mbps} Mbps</td>
      <td class="w-act"><button class="btn-ghost" data-copy="${esc(r.ip)}" title="复制 IP">复制</button></td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll("[data-copy]").forEach((b) => {
    b.onclick = () => {
      navigator.clipboard.writeText(b.dataset.copy).then(
        () => toast("已复制 " + b.dataset.copy, "ok"),
        () => toast("复制失败", "err"));
    };
  });
}

function loadLatest() {
  api("/api/status").then((d) => {
    if (d.result) {
      lastResult = d.result;
      lastResultSource = "latest";
      renderResults();
    }
  }).catch(() => {});
}

/* ═══ 历史 ═══ */

function refreshHistory() {
  api("/api/history").then((list) => {
    const wrap = $("#histList");
    const empty = $("#histEmpty");
    wrap.innerHTML = "";
    $("#histCount").textContent = list.length ? `共 ${list.length} 条（保留最近 50 条）` : "";
    if (!list.length) {
      wrap.appendChild(empty);
      return;
    }
    for (const h of list) {
      const p = h.params || {};
      const modeTxt = h.mode === "DC" ? "指定节点 " + (h.colo || p.colo || "")
        : h.mode === "DC+随机" ? "指定节点 + 随机回退" : "全局随机";
      const item = document.createElement("div");
      item.className = "hist-item";
      item.innerHTML = `
        <div class="hist-main">
          <div class="hist-time mono">${esc(h.time)}${h.cancelled ? ' <span class="cancelled-tag">（已取消）</span>' : ""}</div>
          <div class="hist-params">${esc(modeTxt)} · 测速 ${p.speedSecs}s/${p.speedMB}MB${p.minSpeed ? " · 下限 " + p.minSpeed + "Mbps" : ""} · 用时 ${h.elapsed}s · 返回 ${h.count} 个</div>
        </div>
        <div class="hist-badges">
          <span class="hist-badge">Top ${h.count}</span>
          <span class="hist-badge">${h.results && h.results[0] ? h.results[0].mbps + " Mbps" : "—"}</span>
        </div>
        <div class="hist-ops">
          <button class="btn-ghost" data-act="reuse">复用参数</button>
          <button class="btn-ghost" data-act="csv">CSV</button>
          <button class="btn-ghost danger" data-act="del">删除</button>
        </div>`;
      item.querySelector('[data-act="reuse"]').onclick = () => {
        state.mode = p.mode === "DC" ? "DC" : "RANDOM";
        state.colo = p.colo || "";
        state.randomCount = p.randomCount || 150;
        state.speedSecs = p.speedSecs || 8;
        state.speedMB = p.speedMB || 50;
        state.minSpeed = p.minSpeed || 0;
        setMode(state.mode);
        $("#selDC").value = state.colo;
        $("#inRandCount").value = state.randomCount;
        $("#inSecs").value = state.speedSecs;
        $("#inMB").value = state.speedMB;
        $("#inMinSpeed").value = state.minSpeed;
        ["#inRandCount", "#inSecs", "#inMB"].forEach((id) => {
          const el = $(id);
          el.dispatchEvent(new Event("input"));
        });
        toast("已复用参数", "ok");
      };
      item.querySelector('[data-act="csv"]').onclick = () => {
        window.location = `/api/export?fmt=csv&source=history&history_id=${h.id}`;
      };
      item.querySelector('[data-act="del"]').onclick = async () => {
        try {
          await api("/api/history", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "delete", id: h.id }),
          });
          toast("已删除", "ok");
          refreshHistory();
        } catch (e) { toast(e.message, "err"); }
      };
      wrap.appendChild(item);
    }
  }).catch(() => {});
}

/* ═══ IP 池管理 ═══ */

function refreshPools() {
  api("/api/pools").then((list) => {
    const wrap = $("#poolList");
    wrap.innerHTML = "";
    let total = 0, dcs = 0;
    for (const p of list) {
      total += p.size; dcs++;
      const row = document.createElement("div");
      row.className = "pool-row";
      row.innerHTML = `
        <div class="pool-row-head">
          <span class="pool-code">${esc(p.code)}</span>
          <span class="pool-name">${esc(p.cc_zh || "")} · ${p.size} 个 IP${p.expired ? ' <span class="expired-tag">（已过期）</span>' : ""}</span>
          <button class="btn-ghost danger" data-clear="${esc(p.code)}">清空</button>
        </div>
        <div class="pool-ips">
          ${p.ips.map((ip) => `<span class="pool-ip-badge" data-del="${esc(p.code)}|${esc(ip)}">${esc(ip)}<span class="pool-ip-del">✕</span></span>`).join("")}
        </div>`;
      row.querySelector("[data-clear]").onclick = async () => {
        try {
          await api("/api/pools", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "clear", code: p.code }),
          });
          toast(`已清空 ${p.code} 池`, "ok");
          refreshPools(); refreshDataStatus();
        } catch (e) { toast(e.message, "err"); }
      };
      row.querySelectorAll("[data-del]").forEach((b) => {
        b.onclick = async () => {
          const [code, ip] = b.dataset.del.split("|");
          try {
            await api("/api/pools", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: "remove_ip", code, ip }),
            });
            toast(`已删除 ${ip}`, "ok");
            refreshPools(); refreshDataStatus();
          } catch (e) { toast(e.message, "err"); }
        };
      });
      wrap.appendChild(row);
    }
    $("#poolStats").textContent = list.length ? `${dcs} 个节点 · ${total} 个 IP` : "池为空";
  }).catch(() => {});
}

/* ═══ 系统信息 ═══ */

function openInfo() {
  api("/api/data-status").then((d) => {
    $("#infoBody").innerHTML = `
      <div class="info-kv">
        <div class="kv"><span>版本</span><b class="mono">v${d.version}</b></div>
        <div class="kv"><span>Python</span><b class="mono">${d.python}</b></div>
        <div class="kv"><span>数据目录</span><b class="mono" style="word-break:break-all">${esc(d.data_dir)}</b></div>
        <div class="kv"><span>官方 CF 段</span><b class="mono">${d.cf_cidrs} 条（${d.cf_ts ? fmtAgo(d.cf_ts) : "未获取"}）</b></div>
        <div class="kv"><span>外部 443 清单</span><b class="mono">${d.ext_ips ? d.ext_ips.toLocaleString() : 0} 条（${d.ext_ts ? fmtAgo(d.ext_ts) : "未获取"}）</b></div>
        <div class="kv"><span>IP 池</span><b class="mono">${d.pool_dc} 节点 / ${d.pool_ips} IP${d.pool_expired ? "（已过期）" : ""}</b></div>
        <div class="kv"><span>已知节点</span><b class="mono">${d.colo_count}</b></div>
        <div class="kv"><span>当前扫描</span><b>${d.running ? "运行中" : "空闲"}</b></div>
      </div>
      <p class="hint" style="margin-top:12px">
        固定口径：IPv4 · 443/TLS · 结果 5 个 · 全部流量直连（启动时清除代理环境变量）。<br>
        数据源：cloudflare.com/ips-v4（官方段）+ zip.cm.edu.kg/all.txt（外部 443 清单，7 天缓存）。
      </p>`;
    $("#infoModal").hidden = false;
  }).catch(() => {});
}

/* ═══ 扫描控制 ═══ */

function startScan() {
  if (state.mode === "DC") {
    state.colo = ($("#selDC").value || "").trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(state.colo)) {
      toast("请选择有效的节点代码（如 HKG）", "err");
      return;
    }
  }
  localLogs = [];
  renderLogsFull();
  api("/api/scan", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      mode: state.mode, colo: state.colo,
      randomCount: state.randomCount, speedSecs: state.speedSecs,
      speedMB: state.speedMB, minSpeed: state.minSpeed,
    }),
  }).then(() => {
    toast("扫描已启动", "ok");
  }).catch((e) => toast(e.message, "err"));
}

/* ═══ 初始化 ═══ */

function initControls() {
  $("#segDC").onclick = () => setMode("DC");
  $("#segRand").onclick = () => setMode("RANDOM");
  $("#selDC").onchange = () => { state.colo = $("#selDC").value; };
  $("#dcSearch").oninput = () => fillDCSelect($("#dcSearch").value);
  bindRange("#inRandCount", "randomCount", "#valRand", [10, 2000]);
  bindRange("#inSecs", "speedSecs", "#valSecs", [3, 60]);
  bindRange("#inMB", "speedMB", "#valMB", [10, 1000]);
  $("#inMinSpeed").onchange = () => {
    state.minSpeed = Math.max(0, Math.min(10000, +$("#inMinSpeed").value || 0));
  };
  $("#btnScan").onclick = startScan;
  $("#btnCancel").onclick = async () => {
    if (!confirm("确定取消当前扫描？（保留已测出的部分结果）")) return;
    try {
      await api("/api/cancel", { method: "POST" });
      toast("已发送取消请求", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  // tabs
  $$(".tab-btn").forEach((b) => {
    b.onclick = () => {
      $$(".tab-btn").forEach((x) => x.classList.toggle("on", x === b));
      $$(".tab").forEach((t) => t.classList.toggle("show", t.id === "tab-" + b.dataset.tab));
    };
  });
  // 结果排序
  $("#resSort").onchange = () => {
    resSortKey = $("#resSort").value;
    resSortAsc = true;
    renderResults();
  };
  $$("#resTable th.sortable").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.k;
      if (resSortKey === k) resSortAsc = !resSortAsc;
      else { resSortKey = k; resSortAsc = true; }
      $$("#resTable th.sortable").forEach((x) => x.classList.toggle("on", x === th));
      renderResults();
    };
  });
  // 导出
  $("#btnDlCsv").onclick = () => {
    if (!lastResult || !lastResult.results || !lastResult.results.length) {
      toast("暂无可导出的结果", "err");
      return;
    }
    window.location = "/api/export?fmt=csv&source=latest";
  };
  // 历史
  $("#btnClearHist").onclick = async () => {
    if (!confirm("确定清空全部历史记录？")) return;
    try {
      await api("/api/history", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "clear" }),
      });
      toast("历史已清空", "ok");
      refreshHistory();
    } catch (e) { toast(e.message, "err"); }
  };
  // IP 池弹窗
  $("#btnPools").onclick = () => { $("#poolsModal").hidden = false; refreshPools(); };
  $("#btnClosePools").onclick = () => { $("#poolsModal").hidden = true; };
  $("#poolsModal").onclick = (e) => { if (e.target.id === "poolsModal") $("#poolsModal").hidden = true; };
  $("#btnPoolAdd").onclick = async () => {
    const ips = $("#poolIps").value;
    const code = $("#poolDC").value;
    if (!ips.trim()) { toast("请输入 IP 列表", "err"); return; }
    try {
      const r = await api("/api/pools", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", ips, code }),
      });
      const lines = [
        `入池 ${r.added} · 拒绝 ${r.rejected} · 不符 ${r.mismatch} · 失败 ${r.failed}`,
        Object.keys(r.by_colo || {}).length
          ? "归池：" + Object.entries(r.by_colo).map(([c, l]) => `${c} +${l.length}`).join("，")
          : "",
        (r.details || []).filter((d) => !d.ok).slice(0, 8)
          .map((d) => `✘ ${d.ip}：${d.reason}`).join("\n"),
      ].filter(Boolean).join("\n");
      const pre = $("#poolProbeResult");
      pre.textContent = lines;
      pre.hidden = false;
      toast(`探测完成：入池 ${r.added} 个`, "ok");
      refreshPools(); refreshDataStatus();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#btnPoolClearAll").onclick = async () => {
    if (!confirm("确定清空全部 IP 池？")) return;
    try {
      await api("/api/pools", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "clear_all" }),
      });
      toast("池已清空", "ok");
      refreshPools(); refreshDataStatus();
    } catch (e) { toast(e.message, "err"); }
  };
  // 系统信息
  $("#btnInfo").onclick = openInfo;
  $("#btnCloseInfo").onclick = () => { $("#infoModal").hidden = true; };
  $("#infoModal").onclick = (e) => { if (e.target.id === "infoModal") $("#infoModal").hidden = true; }
}

function init() {
  initControls();
  setMode(state.mode);
  refreshColos();
  refreshDataStatus();
  refreshHistory();
  loadLatest();
  openSSE();
  // 若服务启动时扫描已在运行（刷新页面场景），SSE 首帧即携带当前状态
  // 数据状态低频刷新（扫描进行中由 SSE 携带池统计，无需轮询）
  setInterval(() => {
    if (!$("#runInd").classList.contains("busy")) refreshDataStatus();
  }, 60000);
}

async function refreshColos() {
  try {
    coloGroups = await api("/api/colos");
    fillDCSelect("");
  } catch (e) { /* 静默 */ }
}

document.addEventListener("DOMContentLoaded", init);
