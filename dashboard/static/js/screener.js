/**
 * screener.js — NSE ATH Screener frontend logic
 *
 * Responsibilities
 * ─────────────────
 *   • Trigger background scan via /api/screener/run
 *   • Poll /api/screener/status until done, updating the progress bar
 *   • Render two sortable tables: ATH stocks & 50% ATH alert zone
 *   • Bulk-add to TradingView watchlist via /api/tv/watchlist/bulk
 *   • Bulk-create TV price alerts via /api/tv/alert/bulk
 *   • Reflect TV Desktop connection status from /api/tv/status
 *   • CSV export for both tables
 */

// ── State ─────────────────────────────────────────────────────────────────────

let _athData   = [];   // ATH 90d stocks
let _alertData = [];   // near 50% ATH stocks
let _allData   = [];   // full scan result

let _athSort   = { col: "days_since_ath", asc: true };
let _alertSort = { col: "dist_from_50pct", asc: true };

let _universe  = "nifty50";
let _tvConn    = false;
let _pollTimer = null;

// ── Boot ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initUniverseButtons();
  refreshTvStatus();
});

// ── Universe selector ─────────────────────────────────────────────────────────

function initUniverseButtons() {
  document.querySelectorAll(".uni-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".uni-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _universe = btn.dataset.universe;
    });
  });
}

// ── Scan lifecycle ────────────────────────────────────────────────────────────

// Global — called by onclick="runScan()" (normal) or onclick="runScan(true)" (Force Refresh)
async function runScan(force = false) {
  setRunning(true);

  try {
    const resp = await fetch("/api/screener/run", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ universe: _universe, force }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const { job_id } = await resp.json();
    pollStatus(job_id);
  } catch (err) {
    setRunning(false);
    toast("Scan failed: " + err.message, "err");
  }
}

function pollStatus(jobId) {
  clearTimeout(_pollTimer);

  _pollTimer = setTimeout(async () => {
    try {
      const resp = await fetch(`/api/screener/status/${jobId}`);
      const data = await resp.json();

      if (data.status === "running" || data.status === "pending") {
        updateProgress(data.progress, data.total);
        pollStatus(jobId);   // keep polling — do NOT setRunning(false) yet
        return;
      }

      // Scan finished
      setRunning(false);

      if (data.status === "done" && data.results) {
        renderResults(data.results, data.from_cache, data.cache_age_sec);
      } else {
        toast("Scan returned no results", "err");
      }
    } catch (err) {
      setRunning(false);
      toast("Poll error: " + err.message, "err");
    }
  }, 800);
}

function setRunning(running) {
  const btn      = document.getElementById("runBtn");
  const forceBtn = document.getElementById("forceBtn");
  const wrap     = document.getElementById("progressWrap");
  btn.disabled        = running;
  btn.textContent     = running ? "⏳ Scanning…" : "▶ Run Scan";
  if (forceBtn) forceBtn.disabled = running;
  wrap.classList.toggle("show", running);
  if (!running) updateProgress(0, 0);
}

function updateProgress(done, total) {
  const fill  = document.getElementById("progressFill");
  const label = document.getElementById("progressLabel");
  const pct   = total > 0 ? Math.round(done / total * 100) : 0;
  fill.style.width    = pct + "%";
  label.textContent   = total > 0 ? `${done} / ${total}` : "";
}

// ── Render results ────────────────────────────────────────────────────────────

function renderResults(results, fromCache = false, cacheAgeSec = null) {
  _athData   = results.ath_90d      ?? [];
  _alertData = results.alert_50pct  ?? [];
  _allData   = results.all          ?? [];

  // Summary cards
  const cards = document.getElementById("summaryCards");
  cards.style.display = "grid";

  // "Stocks scanned" — show returned/universe so a shortfall is visible
  // at a glance instead of just showing whatever count came back.
  const universeSize = results.universe_size ?? _allData.length;
  const returned      = results.returned_count ?? _allData.length;
  const totalEl = document.getElementById("cardTotal");
  totalEl.textContent = universeSize > returned ? `${returned} / ${universeSize}` : `${returned}`;
  totalEl.title = universeSize > returned
    ? `${universeSize} symbols in this universe, ${returned} returned data`
    : "";

  const failedCount = results.failed_count ?? 0;
  const failedEl = document.getElementById("cardFailed");
  failedEl.textContent = failedCount;

  document.getElementById("cardAth").textContent    = _athData.length;
  document.getElementById("cardAlert").textContent  = _alertData.length;
  const ts = results.scanned_at
    ? new Date(results.scanned_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })
    : "—";
  document.getElementById("cardTime").textContent   = ts;

  // Cache freshness note — makes it obvious when "Run Scan" just handed back
  // an old cached result instead of actually re-fetching anything.
  const cacheNoteEl = document.getElementById("cardCacheNote");
  if (cacheNoteEl) {
    if (fromCache) {
      const mins = cacheAgeSec != null ? Math.round(cacheAgeSec / 60) : null;
      cacheNoteEl.textContent = mins != null
        ? `📦 cached, fetched ${mins} min ago — use Force Refresh for live data`
        : "📦 cached results";
    } else {
      cacheNoteEl.textContent = "✓ freshly fetched";
    }
  }

  // Failed-symbols panel
  renderFailedPanel(results.failed ?? []);

  // Badge counts
  document.getElementById("athCount").textContent   = _athData.length;
  document.getElementById("alertCount").textContent = _alertData.length;

  renderAthTable();
  renderAlertTable();
  updateActionButtons();
}

function renderFailedPanel(failed) {
  const panel = document.getElementById("failedPanel");
  const list  = document.getElementById("failedList");
  const summaryText = document.getElementById("failedSummaryText");
  if (!panel || !list) return;

  if (!failed.length) {
    panel.style.display = "none";
    list.innerHTML = "";
    return;
  }

  panel.style.display = "block";
  summaryText.textContent = `${failed.length} symbol${failed.length === 1 ? "" : "s"} failed to fetch`;
  list.innerHTML = failed.map(f =>
    `<div style="padding:2px 0"><b>${esc(f.symbol.replace(".NS",""))}</b> — ${esc(f.name)}: <span style="color:var(--text-3)">${esc(f.reason)}</span></div>`
  ).join("");
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── ATH Table ─────────────────────────────────────────────────────────────────

function renderAthTable() {
  const sorted = [..._athData].sort(makeSorter(_athSort));
  const wrap   = document.getElementById("athTableWrap");

  if (!sorted.length) {
    wrap.innerHTML = emptyHTML("No stocks made ATH in the last 90 days");
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th style="width:24px"></th>
          <th data-col="symbol"        data-tbl="ath">Symbol</th>
          <th data-col="name"          data-tbl="ath">Company</th>
          <th data-col="current_price" data-tbl="ath">LTP ₹</th>
          <th data-col="all_time_high" data-tbl="ath">ATH ₹</th>
          <th data-col="ath_date"      data-tbl="ath">ATH Date</th>
          <th data-col="days_since_ath"data-tbl="ath">Days</th>
          <th data-col="pct_from_ath"  data-tbl="ath">% from ATH</th>
          <th data-col="day_change_pct"data-tbl="ath">Day %</th>
          <th data-col="w52_high"      data-tbl="ath">52W High</th>
          <th data-col="volume_ratio"  data-tbl="ath">Volume</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map(r => athRow(r)).join("")}
      </tbody>
    </table>`;

  wrap.querySelectorAll("thead th[data-col]").forEach(th =>
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (_athSort.col === col) _athSort.asc = !_athSort.asc;
      else { _athSort.col = col; _athSort.asc = true; }
      renderAthTable();
    })
  );

  markSortHeader(wrap, _athSort);
}

function athRow(r) {
  const chgClass = r.day_change_pct >= 0 ? "td-pos" : "td-neg";
  const pctClass = r.pct_from_ath  >= 0 ? "td-pos" : "td-neg";
  return `<tr>
    <td><input type="checkbox" class="row-check ath-check" data-symbol="${r.symbol}" data-price="${r.all_time_high}"></td>
    <td class="td-sym">${r.symbol.replace(".NS","")}</td>
    <td class="td-name" title="${r.name}">${r.name}</td>
    <td class="td-price">₹${fmt(r.current_price)}</td>
    <td class="td-num">₹${fmt(r.all_time_high)}</td>
    <td class="td-num">${r.ath_date}</td>
    <td class="td-num"><span class="ath-pill">🏆 ${r.days_since_ath}d</span></td>
    <td class="${pctClass}">${r.pct_from_ath}%</td>
    <td class="${chgClass}">${r.day_change_pct >= 0 ? "+" : ""}${r.day_change_pct}%</td>
    <td class="td-num">₹${fmt(r.w52_high)}</td>
    <td class="td-num">${volCell(r)}</td>
  </tr>`;
}

// ── 50% ATH Alert Table ───────────────────────────────────────────────────────

function renderAlertTable() {
  const sorted = [..._alertData].sort(makeSorter(_alertSort));
  const wrap   = document.getElementById("alertTableWrap");

  if (!sorted.length) {
    wrap.innerHTML = emptyHTML("No stocks near the 50 % ATH level");
    return;
  }

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th style="width:24px"></th>
          <th data-col="symbol"        data-tbl="alert">Symbol</th>
          <th data-col="name"          data-tbl="alert">Company</th>
          <th data-col="current_price" data-tbl="alert">LTP ₹</th>
          <th data-col="all_time_high" data-tbl="alert">ATH ₹</th>
          <th data-col="half_ath_price"data-tbl="alert">50% ATH ₹</th>
          <th data-col="dist_from_50pct" data-tbl="alert">Distance</th>
          <th data-col="pct_from_ath"  data-tbl="alert">% from ATH</th>
          <th data-col="day_change_pct"data-tbl="alert">Day %</th>
          <th data-col="days_since_ath"data-tbl="alert">ATH Age</th>
          <th data-col="volume_ratio"  data-tbl="alert">Volume</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map(r => alertRow(r)).join("")}
      </tbody>
    </table>`;

  wrap.querySelectorAll("thead th[data-col]").forEach(th =>
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (_alertSort.col === col) _alertSort.asc = !_alertSort.asc;
      else { _alertSort.col = col; _alertSort.asc = true; }
      renderAlertTable();
    })
  );

  markSortHeader(wrap, _alertSort);
}

function alertRow(r) {
  const chgClass = r.day_change_pct >= 0 ? "td-pos" : "td-neg";
  const pctClass = r.pct_from_ath  >= 0 ? "td-pos" : "td-neg";
  const distCls  = Math.abs(r.dist_from_50pct) <= 2 ? "td-warn" : "td-num";
  return `<tr>
    <td><input type="checkbox" class="row-check alert-check" data-symbol="${r.symbol}" data-price="${r.half_ath_price}"></td>
    <td class="td-sym">${r.symbol.replace(".NS","")}</td>
    <td class="td-name" title="${r.name}">${r.name}</td>
    <td class="td-price">₹${fmt(r.current_price)}</td>
    <td class="td-num">₹${fmt(r.all_time_high)}</td>
    <td class="td-num"><span class="near-pill">🔔 ₹${fmt(r.half_ath_price)}</span></td>
    <td class="${distCls}">${r.dist_from_50pct >= 0 ? "+" : ""}${r.dist_from_50pct}%</td>
    <td class="${pctClass}">${r.pct_from_ath}%</td>
    <td class="${chgClass}">${r.day_change_pct >= 0 ? "+" : ""}${r.day_change_pct}%</td>
    <td class="td-num">${ageCell(r)}</td>
    <td class="td-num">${volCell(r)}</td>
  </tr>`;
}

// ── Selection helpers ─────────────────────────────────────────────────────────

window.selectAll = function(type) {
  const cls = type === "ath" ? ".ath-check" : ".alert-check";
  const boxes = document.querySelectorAll(cls);
  const allChecked = [...boxes].every(b => b.checked);
  boxes.forEach(b => { b.checked = !allChecked; });
  updateActionButtons();
};

function getChecked(cls) {
  return [...document.querySelectorAll(cls)]
    .filter(b => b.checked)
    .map(b => ({ symbol: b.dataset.symbol, price: parseFloat(b.dataset.price) }));
}

function updateActionButtons() {
  document.getElementById("addWatchlistBtn").disabled =
    !_tvConn || getChecked(".ath-check").length === 0;
  document.getElementById("createAlertsBtn").disabled =
    !_tvConn || getChecked(".alert-check").length === 0;
}

document.addEventListener("change", e => {
  if (e.target.classList.contains("row-check")) updateActionButtons();
});

// ── TV Integration ────────────────────────────────────────────────────────────

window.refreshTvStatus = async function() {
  try {
    const resp = await fetch("/api/tv/status");
    const info = await resp.json();
    _tvConn = info.connected;

    const badge     = document.getElementById("tvBadge");
    const badgeTxt  = document.getElementById("tvBadgeText");
    const connView  = document.getElementById("tvConnectedView");
    const discoView = document.getElementById("tvDisconnectedView");
    const syncBtn   = document.getElementById("syncStudiesBtn");

    if (_tvConn) {
      badge.className   = "tv-badge connected";
      badgeTxt.textContent = "TV Desktop ✓";
      connView.style.display  = "block";
      discoView.style.display = "none";
      syncBtn.disabled  = false;
    } else {
      badge.className   = "tv-badge disconnected";
      badgeTxt.textContent = "TV Desktop ✗";
      connView.style.display  = "none";
      discoView.style.display = "block";
      syncBtn.disabled  = true;
    }
    updateActionButtons();
  } catch {}
};

window.syncStudies = async function() {
  const btn = document.getElementById("syncStudiesBtn");
  btn.textContent = "Syncing…";
  btn.disabled    = true;

  try {
    const resp    = await fetch("/api/tv/studies");
    const json    = await resp.json();
    const studies = json.studies ?? [];

    const list = document.getElementById("studiesList");
    if (studies.length === 0) {
      list.innerHTML = `<span style="color:var(--text-3);font-size:11px">No studies on current TV chart</span>`;
    } else {
      list.innerHTML = studies
        .map(s => `<span class="study-chip">${s.name || s.id}</span>`)
        .join("");
    }
    toast(`Synced ${studies.length} studies from TradingView`, "ok");
  } catch (err) {
    toast("Sync failed: " + err.message, "err");
  } finally {
    btn.textContent = "Sync Studies";
    btn.disabled    = !_tvConn;
  }
};

// ── Bulk watchlist ────────────────────────────────────────────────────────────

window.bulkAddWatchlist = async function(type) {
  const cls    = type === "ath" ? ".ath-check" : ".alert-check";
  const items  = getChecked(cls);
  if (!items.length) return;

  const btn    = document.getElementById("addWatchlistBtn");
  btn.textContent = "Adding…";
  btn.disabled    = true;

  try {
    const resp = await fetch("/api/tv/watchlist/bulk", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ symbols: items.map(i => i.symbol) }),
    });
    const json = await resp.json();
    toast(`Added ${json.added} symbols to TV watchlist${json.failed ? ` (${json.failed} failed)` : ""}`, "ok");
  } catch (err) {
    toast("Watchlist error: " + err.message, "err");
  } finally {
    btn.textContent = "+ TV Watchlist";
    updateActionButtons();
  }
};

// ── Bulk alerts ───────────────────────────────────────────────────────────────

window.bulkCreateAlerts = async function() {
  const items = getChecked(".alert-check");
  if (!items.length) return;

  const btn   = document.getElementById("createAlertsBtn");
  btn.textContent = "Creating…";
  btn.disabled    = true;

  const alertList = items.map(i => ({
    symbol:    i.symbol,
    price:     i.price,
    condition: "crossing",
    message:   `${i.symbol.replace(".NS","")} — 50% of ATH: ₹${fmt(i.price)}`,
  }));

  try {
    const resp = await fetch("/api/tv/alert/bulk", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ alerts: alertList }),
    });
    const json = await resp.json();
    toast(`Created ${json.created} TV alerts${json.failed ? ` (${json.failed} failed)` : ""}`, "ok");
  } catch (err) {
    toast("Alert error: " + err.message, "err");
  } finally {
    btn.textContent = "🔔 Create TV Alerts";
    updateActionButtons();
  }
};

// ── CSV export ────────────────────────────────────────────────────────────────

window.exportCSV = function(type) {
  const data = type === "ath" ? _athData : _alertData;
  if (!data.length) { toast("No data to export", "err"); return; }

  const headers = Object.keys(data[0]);
  const rows    = data.map(r => headers.map(h => JSON.stringify(r[h] ?? "")).join(","));
  const csv     = [headers.join(","), ...rows].join("\n");
  const blob    = new Blob([csv], { type: "text/csv" });
  const url     = URL.createObjectURL(blob);
  const a       = document.createElement("a");
  a.href        = url;
  a.download    = `nse_${type}_scan_${dateSuffix()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
};

// ── Sort helpers ──────────────────────────────────────────────────────────────

function makeSorter({ col, asc }) {
  return (a, b) => {
    const av = a[col], bv = b[col];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    return asc ? av - bv : bv - av;
  };
}

function markSortHeader(wrap, sortState) {
  wrap.querySelectorAll("thead th[data-col]").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.col === sortState.col)
      th.classList.add(sortState.asc ? "sort-asc" : "sort-desc");
  });
}

// ── Misc helpers ──────────────────────────────────────────────────────────────

function fmt(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

// Compact Indian-style volume: 12.3L (lakh), 1.2Cr (crore), else raw with commas.
function fmtVol(n) {
  if (n == null) return "—";
  const v = Number(n);
  if (v >= 1e7) return (v / 1e7).toFixed(1) + "Cr";
  if (v >= 1e5) return (v / 1e5).toFixed(1) + "L";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(v);
}

// Human-readable "how old is this ATH" label from days_since_ath.
// STALE_ATH_DAYS marks stocks whose ATH is old enough (~2 years) that a
// touch of the 50% level is more likely a structurally de-rated stock than
// a genuine retracement of a recent move — flagged so it's visible at a
// glance rather than needing to cross-check the raw date.
const STALE_ATH_DAYS = 730;

function ageLabel(days) {
  if (days == null) return "—";
  if (days < 30)  return `${days}d`;
  if (days < 365) return `${Math.round(days / 30)}mo`;
  return `${(days / 365).toFixed(1)}yr`;
}

function ageCell(r) {
  const days  = r.days_since_ath;
  const label = ageLabel(days);
  if (days != null && days > STALE_ATH_DAYS) {
    return `<span class="age-pill age-stale" title="ATH set over 2 years ago — this may be a de-rated stock rather than a fresh retracement">⚠ ${label}</span>`;
  }
  return label;
}

// VOL_SPIKE_RATIO: latest volume vs its own 20-session average. A touch of
// the 50% ATH level on ≥1.5x average volume is a much stronger tell than
// one drifting past on thin volume — flag it so it stands out in the table.
const VOL_SPIKE_RATIO = 1.5;

function volCell(r) {
  const vol   = fmtVol(r.volume);
  const ratio = r.volume_ratio;
  if (ratio != null && ratio >= VOL_SPIKE_RATIO) {
    return `<span class="vol-pill" title="${ratio}x the 20-session average volume">🔥 ${vol}</span>`;
  }
  return vol;
}

function dateSuffix() {
  return new Date().toISOString().slice(0, 10);
}

function emptyHTML(msg) {
  return `<div class="empty-state">
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.5">
      <circle cx="11" cy="11" r="8"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    ${msg}
  </div>`;
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type = "ok") {
  const area = document.getElementById("toastArea");
  const el   = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  area.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, 3500);
}
