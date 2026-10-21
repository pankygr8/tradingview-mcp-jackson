/* gann_fan.js — Gann Fan Angle Level Scanner UI */
"use strict";

// ── State ─────────────────────────────────────────────────────────────────────
let _universe = "nifty50";
let _tf       = "1d";
let _filter   = "near";   // near | all | buy | sell | both
let _rows     = [];        // full result from last scan
let _sort     = { col: "nearest_pct", asc: true };
let _pollTimer = null;

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initUniverseBtns();
  initTfToggle();
  initFilterTabs();
  checkTV();
});

// ── TV badge ──────────────────────────────────────────────────────────────────
function checkTV() {
  fetch("/api/tv/status")
    .then(r => r.json())
    .then(d => {
      const badge = document.getElementById("tvBadge");
      const text  = document.getElementById("tvBadgeText");
      if (d.connected) {
        badge.classList.add("connected");
        text.textContent = "TV Connected";
      } else {
        text.textContent = "TV Offline";
      }
    })
    .catch(() => {
      document.getElementById("tvBadgeText").textContent = "TV Offline";
    });
}

// ── Universe buttons ──────────────────────────────────────────────────────────
function initUniverseBtns() {
  document.querySelectorAll("#universeBtns .uni-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#universeBtns .uni-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _universe = btn.dataset.universe;
    });
  });
}

// ── Timeframe toggle ──────────────────────────────────────────────────────────
function initTfToggle() {
  document.querySelectorAll("#tfToggle button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#tfToggle button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _tf = btn.dataset.tf;
    });
  });
}

// ── Filter tabs ───────────────────────────────────────────────────────────────
function initFilterTabs() {
  document.querySelectorAll(".ftab[data-f]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".ftab").forEach(b => {
        b.classList.remove("active", "buy-active", "sell-active");
      });
      _filter = btn.dataset.f;
      const cls = _filter === "buy" ? "buy-active" : _filter === "sell" ? "sell-active" : "active";
      btn.classList.add(cls);
      renderTable();
    });
  });
}

// ── Run scan ──────────────────────────────────────────────────────────────────
function runScan() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }

  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  btn.textContent = "⏳ Scanning…";

  showProgress(true, 0, 0);

  fetch("/api/gann/run", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ universe: _universe, timeframe: _tf }),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.job_id) throw new Error("No job_id returned");
      pollStatus(d.job_id);
    })
    .catch(err => {
      toast("Scan error: " + err.message, "error");
      btn.disabled = false;
      btn.textContent = "▶ Run Scan";
      showProgress(false);
    });
}

function pollStatus(jobId) {
  _pollTimer = setInterval(() => {
    fetch(`/api/gann/status/${jobId}`)
      .then(r => r.json())
      .then(d => {
        if (d.status === "running") {
          showProgress(true, d.progress, d.total);
          return;
        }
        clearInterval(_pollTimer); _pollTimer = null;
        showProgress(false);
        const btn = document.getElementById("runBtn");
        btn.disabled = false;
        btn.textContent = "▶ Run Scan";

        if (d.status === "done" && d.results) {
          onResults(d.results);
        } else {
          toast("Scan failed or no results", "error");
        }
      })
      .catch(() => {});
  }, 800);
}

// ── Progress bar ──────────────────────────────────────────────────────────────
function showProgress(visible, done = 0, total = 0) {
  const wrap  = document.getElementById("progressWrap");
  const fill  = document.getElementById("progressFill");
  const label = document.getElementById("progressLabel");
  if (!visible) {
    wrap.classList.remove("show");
    return;
  }
  wrap.classList.add("show");
  const pct = total > 0 ? Math.round(done / total * 100) : 0;
  fill.style.width  = pct + "%";
  label.textContent = `${done} / ${total}`;
}

// ── Process results ───────────────────────────────────────────────────────────
function onResults(results) {
  // Flatten: all scanned + near-level subsets
  _rows = results.all || [];

  const nearAll = [...(results.buy || []), ...(results.sell || []), ...(results.both || [])];
  const nearCount = nearAll.length;
  const confirmedCount = _rows.filter(r => r.confirmed_signal).length;

  // Summary cards
  document.getElementById("summaryCards").style.display = "flex";
  document.getElementById("cTotal").textContent = _rows.length;
  document.getElementById("cNear").textContent  = nearCount;
  document.getElementById("cConfirmed").textContent = confirmedCount;
  document.getElementById("cBuy").textContent   = (results.buy  || []).length;
  document.getElementById("cSell").textContent  = (results.sell || []).length;
  document.getElementById("cPct").textContent   = `±${results.near_pct_threshold ?? 1}%`;

  const at = results.scanned_at
    ? new Date(results.scanned_at).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
    : "—";
  document.getElementById("cTime").textContent = at;

  // Show filter tabs and legend
  document.getElementById("filterTabs").style.display = "flex";
  document.getElementById("legend").style.display = "block";

  renderTable();
  toast(`Scan complete — ${_rows.length} scanned, ${nearCount} near levels`, "ok");
}

// ── Render table ──────────────────────────────────────────────────────────────
function renderTable() {
  let rows = [..._rows];

  // Apply filter
  if (_filter === "near") {
    rows = rows.filter(r => r.signal !== null && r.signal !== undefined);
  } else if (_filter === "confirmed") {
    rows = rows.filter(r => r.confirmed_signal !== null && r.confirmed_signal !== undefined);
  } else if (_filter === "buy") {
    rows = rows.filter(r => r.signal === "BUY");
  } else if (_filter === "sell") {
    rows = rows.filter(r => r.signal === "SELL");
  } else if (_filter === "both") {
    rows = rows.filter(r => r.signal === "BOTH");
  }
  // "all" → no filter

  // Sort
  rows.sort((a, b) => {
    const av = deepGet(a, _sort.col);
    const bv = deepGet(b, _sort.col);
    if (av == null) return 1;
    if (bv == null) return -1;
    return _sort.asc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });

  const wrap = document.getElementById("tableWrap");

  if (rows.length === 0) {
    wrap.innerHTML = `<div class="empty-state">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      No stocks match the current filter
    </div>`;
    return;
  }

  const cols = [
    { label: "Symbol",       key: "symbol",        w: "100px" },
    { label: "Name",         key: "name",           w: "130px" },
    { label: "Price",        key: "current_price",  w: "80px"  },
    { label: "Chg%",         key: "day_change_pct", w: "55px"  },
    { label: "Angle θ",      key: "main_angle",     w: "70px"  },
    { label: "Signal",       key: "signal",         w: "65px"  },
    { label: "Near Level",   key: "_near",          w: "110px" },
    { label: "Distance",     key: "nearest_pct",    w: "85px"  },
    { label: "H-A (θ/2)",    key: "h_level_a",     w: "90px"  },
    { label: "H-B (θ/4)",    key: "h_level_b",     w: "90px"  },
    { label: "L-A (θ/2)",    key: "l_level_a",     w: "90px"  },
    { label: "L-B (θ/4)",    key: "l_level_b",     w: "90px"  },
    { label: "Swing H",      key: "swing_high",     w: "80px"  },
    { label: "Swing L",      key: "swing_low",      w: "80px"  },
    { label: "Dur (bars)",   key: "sw_dur",         w: "65px"  },
  ];

  let th = cols.map(c => `<th style="min-width:${c.w};cursor:pointer" onclick="sortBy('${c.key}')">${c.label}${sortArrow(c.key)}</th>`).join("");
  let tb = rows.map(r => rowHtml(r)).join("");

  wrap.innerHTML = `
    <table>
      <thead><tr>${th}</tr></thead>
      <tbody>${tb}</tbody>
    </table>`;
}

function rowHtml(r) {
  const sig     = r.signal;
  const trClass = sig === "BUY" ? "tr-buy" : sig === "SELL" ? "tr-sell" : sig === "BOTH" ? "tr-both" : "";

  // Actual angles (different per stock)
  const θ  = r.main_angle    != null ? r.main_angle.toFixed(2)    : "—";
  const θh = r.half_angle    != null ? r.half_angle.toFixed(2)    : "—";
  const θq = r.quarter_angle != null ? r.quarter_angle.toFixed(2) : "—";

  const angleHtml = `<span class="angle-viz"
    title="Main angle θ=${θ}° (from swing geometry)\nθ/2=${θh}° → H-A / L-A levels\nθ/4=${θq}° → H-B / L-B levels">
    <b style="font-family:var(--mono);color:#e67e22">${θ}°</b>
    <span style="color:var(--text-2)">${θh}° / ${θq}°</span>
  </span>`;

  // Signal pill — badge reflects whether ANY near level actually confirmed
  // (touched + reacted + still holding), not just raw proximity.
  const isConfirmed = r.confirmed_signal === sig || r.confirmed_signal === "BOTH";
  const sigBadge = sig ? (isConfirmed
    ? `<span title="Confirmed — price tested a level here and reacted" style="color:var(--green)">✔</span>`
    : `<span title="Unconfirmed — price is near a level but hasn't shown a tested reaction yet" style="color:var(--text-2)">⏳</span>`
  ) : "";
  let sigHtml = "—";
  if (sig === "BUY")  sigHtml = `<span class="sig-buy">▲ BUY</span> ${sigBadge}`;
  else if (sig === "SELL") sigHtml = `<span class="sig-sell">▼ SELL</span> ${sigBadge}`;
  else if (sig === "BOTH") sigHtml = `<span class="sig-both">↕ BOTH</span> ${sigBadge}`;

  // Near level pills — show actual level price, angle, and touch/reaction status
  const nearLevels = r.near_levels || [];
  let nearHtml = nearLevels.length > 0
    ? nearLevels.map(nl => {
        const cls = nl.label === "H-A" ? "lv-h-a" : nl.label === "H-B" ? "lv-h-b"
                  : nl.label === "L-A" ? "lv-l-a" : "lv-l-b";
        const dir     = nl.direction === "buy" ? "▲" : "▼";
        const angLbl  = nl.label.endsWith("A") ? `θ/2=${θh}°` : `θ/4=${θq}°`;

        // Reaction status icon — same semantics as the Swing Fib "Confirm" column:
        //  ✔ touched + reacted + still holding   ⚠ touched + reacted but since broke back through
        //  ✘ touched but never reacted           ⏳ touched too recently to judge, or never tested yet
        let reactIcon, reactTip;
        if (nl.confirmed === true) {
          reactIcon = "✔"; reactTip = `Tested ${nl.bars_since_touch} bar(s) ago and reacted — still holding`;
        } else if (nl.confirmed === false && nl.reaction_ok === false) {
          reactIcon = "✘"; reactTip = `Tested ${nl.bars_since_touch} bar(s) ago — no reaction, price just drifted through`;
        } else if (nl.confirmed === false) {
          reactIcon = "⚠"; reactTip = `Reacted after the test, but price has since broken back through the level`;
        } else if (nl.touched_bar != null) {
          reactIcon = "⏳"; reactTip = `Tested ${nl.bars_since_touch} bar(s) ago — still waiting to see a reaction`;
        } else {
          reactIcon = "⏳"; reactTip = `Currently near this level but hasn't been tested in the recent lookback — no reaction to judge yet`;
        }

        return `<span class="lv-badge ${cls}"
          title="${nl.label} @ ${fmt(nl.price)}\n${angLbl}\n${nl.dist_pct.toFixed(2)}% away · ${nl.direction.toUpperCase()} zone\n${reactTip}">
          ${dir} ${nl.label} ${reactIcon}</span>`;
      }).join(" ")
    : `<span style="color:var(--text-2);font-size:10px">—</span>`;

  // Distance bar
  const nearPct = r.nearest_pct ?? 100;
  const barFill = Math.min(100 - nearPct * 90, 100);
  const barCls  = nearPct < 0.3 ? "dist-close" : nearPct < 0.7 ? "dist-medium" : "dist-far";

  const distHtml = r.signal
    ? `<span class="dist-bar-wrap"><span class="dist-bar-fill ${barCls}" style="width:${barFill.toFixed(0)}%"></span></span>
       <span style="font-size:10px;margin-left:4px">${nearPct.toFixed(2)}%</span>`
    : `<span style="color:var(--text-2);font-size:10px">${nearPct.toFixed(2)}%</span>`;

  const chgCls = r.day_change_pct > 0 ? "green" : r.day_change_pct < 0 ? "red" : "";
  const chgPfx = r.day_change_pct > 0 ? "+" : "";

  // Source indicator + tooltip showing all 3 angles
  const srcHtml = r.recent_source === "high"
    ? `<span class="src-high" title="Fan from swing HIGH (angles go down-right)">▼H</span>`
    : `<span class="src-low"  title="Fan from swing LOW (angles go up-right)">▲L</span>`;

  return `<tr class="${trClass}">
    <td><b>${tickerLabel(r.symbol)}</b> ${srcHtml}</td>
    <td>${r.name}</td>
    <td style="font-family:var(--mono)">${fmt(r.current_price)}</td>
    <td class="${chgCls}" style="font-family:var(--mono)">${chgPfx}${r.day_change_pct}%</td>
    <td>${angleHtml}</td>
    <td>${sigHtml}</td>
    <td>${nearHtml}</td>
    <td>${distHtml}</td>
    <td><span class="lv-badge lv-h-a" title="High-fan θ/2=${θh}° level">${fmt(r.h_level_a)}</span></td>
    <td><span class="lv-badge lv-h-b" title="High-fan θ/4=${θq}° level">${fmt(r.h_level_b)}</span></td>
    <td><span class="lv-badge lv-l-a" title="Low-fan θ/2=${θh}° level">${fmt(r.l_level_a)}</span></td>
    <td><span class="lv-badge lv-l-b" title="Low-fan θ/4=${θq}° level">${fmt(r.l_level_b)}</span></td>
    <td style="font-family:var(--mono);color:var(--red)">${fmt(r.swing_high)}</td>
    <td style="font-family:var(--mono);color:var(--green)">${fmt(r.swing_low)}</td>
    <td style="font-family:var(--mono);color:var(--text-2)">${r.sw_dur}</td>
  </tr>`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(v) {
  if (v == null) return "—";
  const n = Number(v);
  if (isNaN(n)) return v;
  // Auto-decimal: crypto/forex need more precision
  if (n > 1000)   return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  if (n > 10)     return n.toFixed(2);
  if (n > 1)      return n.toFixed(4);
  return n.toFixed(5);
}

function tickerLabel(sym) {
  if (!sym) return "";
  return sym.replace(/\.NS$/, "").replace(/=X$/, "").replace(/=F$/, "");
}

function deepGet(obj, key) {
  return key.split(".").reduce((o, k) => (o ? o[k] : undefined), obj);
}

function sortBy(col) {
  if (_sort.col === col) {
    _sort.asc = !_sort.asc;
  } else {
    _sort.col = col;
    _sort.asc = true;
  }
  renderTable();
}

function sortArrow(col) {
  if (_sort.col !== col) return "";
  return _sort.asc ? " ▲" : " ▼";
}

// ── CSV Export ────────────────────────────────────────────────────────────────
function exportCSV() {
  let rows = [..._rows];
  if (_filter === "near")       rows = rows.filter(r => r.signal);
  else if (_filter === "confirmed") rows = rows.filter(r => r.confirmed_signal);
  else if (_filter === "buy")   rows = rows.filter(r => r.signal === "BUY");
  else if (_filter === "sell")  rows = rows.filter(r => r.signal === "SELL");
  else if (_filter === "both")  rows = rows.filter(r => r.signal === "BOTH");

  if (!rows.length) { toast("No rows to export", "warn"); return; }

  const headers = ["Symbol","Name","Price","Chg%","Signal","ConfirmedSignal","NearLevels","NearestPct",
                   "H-A","H-B","L-A","L-B","SwingHigh","SwingLow","SwDur"];
  const lines = [headers.join(",")];
  rows.forEach(r => {
    const nl = (r.near_levels || []).map(n =>
      `${n.label}@${n.price}${n.confirmed === true ? "(confirmed)" : n.confirmed === false ? "(failed)" : "(pending)"}`
    ).join("|");
    lines.push([
      r.symbol, `"${r.name}"`,
      r.current_price, r.day_change_pct, r.signal || "", r.confirmed_signal || "", `"${nl}"`,
      r.nearest_pct, r.h_level_a, r.h_level_b, r.l_level_a, r.l_level_b,
      r.swing_high, r.swing_low, r.sw_dur,
    ].join(","));
  });

  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a    = document.createElement("a");
  a.href     = URL.createObjectURL(blob);
  a.download = `gann_fan_${_tf}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = "ok") {
  const area = document.getElementById("toastArea");
  const t    = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  area.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}
