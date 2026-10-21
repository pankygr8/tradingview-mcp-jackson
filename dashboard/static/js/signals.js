/**
 * signals.js — Dual-Timeframe Swing Fib Angle Signal Scanner
 * v3 — supports Daily (1d) and 15-min (15m) panels independently.
 *
 * HTML contract (signals.html):
 *   #runBtn-{tf}      run button per timeframe
 *   #count-{tf}       signal count badge
 *   #progress-{tf}    .tf-progress-wrap element
 *   #fill-{tf}        progress bar fill div
 *   #label-{tf}       progress label span
 *   #cards-{tf}       mini-cards container
 *   #c{id}-total/buy/sell/time  stat values  (id = "1d" → "1d", "15m" → "15m")
 *   #filter-{tf}      filter button row
 *   #table-{tf}       table-wrap div
 *   .filter-btn[data-tf][data-f]  filter buttons
 *   .uni-btn[data-universe]       universe selector buttons
 */

"use strict";

// ── Per-timeframe state ───────────────────────────────────────────────────────

const TF = ["1d", "15m"];

// Each tf gets its own isolated state object
const _state = {
  "1d": {
    rows:        [],          // all rows with _sig populated
    filter:      "all",       // "all" | "buy" | "sell"
    validOnly:   false,       // show only confirmed signals
    highScore:   false,       // show only quality score ≥ 55
    ptSquare:    false,       // show only stocks near Gann P/T square (score ≥ 30)
    sort:        { col: "_sig.bars_ago", asc: true },
    pollTimer:   null,
  },
  "15m": {
    rows:        [],
    filter:      "all",
    validOnly:   false,
    highScore:   false,
    ptSquare:    false,       // show only stocks near Gann P/T square (score ≥ 30)
    gannIntraday: false,      // show only stocks in Gann intraday convergence (score ≥ 45)
    sort:        { col: "gann_intraday.score", asc: false }, // default: Gann score first
    pollTimer:   null,
  },
};

// Helper: card element ID prefix (no hyphens inside selector)
// "1d" → "c1d", "15m" → "c15m"
function cardId(tf) { return "c" + tf.replace("-", ""); }

// Universe + mode selectors — shared across both panels
let _universe  = "nifty50";
let _bosMode   = "Both";           // "Simple" | "Strict" | "Both"
let _swingMode = "Largest Range";  // "Largest Range" | "Most Recent"
let _lbBars    = null;             // null → use server default (200); number → override

// ── Auto-poll state (15m only) ────────────────────────────────────────────────
const AUTO_POLL_INTERVAL_MS = 15 * 60 * 1000;  // 15 minutes
let _autoPollOn        = false;
let _autoPollTimer     = null;   // setInterval handle for the scan trigger
let _autoPollCountdown = null;   // setInterval handle for the countdown display
let _autoPollNextAt    = 0;      // epoch ms when next scan fires

// ── Boot ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initUniverseButtons();
  initFilterButtons();
  initToggle("bosModeToggle",   "data-bos",   v => { _bosMode   = v; });
  initToggle("swingModeToggle", "data-swing",  v => { _swingMode = v; });
  // Lookback bars toggle — stores as integer; "200 ✓" button maps to null (= default)
  initToggle("lbBarsToggle", "data-lb", v => {
    const n = parseInt(v, 10);
    // 200 is the Pine default — send null so server uses its built-in default
    _lbBars = (n === 200) ? null : n;
  });
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

// Generic toggle group initialiser
function initToggle(containerId, dataAttr, onChange) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  wrap.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      wrap.querySelectorAll("button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      onChange(btn.getAttribute(dataAttr));
    });
  });
}

// ── Filter buttons ────────────────────────────────────────────────────────────

function initFilterButtons() {
  document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const tf = btn.dataset.tf;   // "1d" or "15m"
      const f  = btn.dataset.f;    // "all", "buy", "sell", "valid"

      if (f === "valid") {
        // Toggle: flip validOnly for this tf
        _state[tf].validOnly = !_state[tf].validOnly;
        btn.classList.toggle("active", _state[tf].validOnly);
      } else if (f === "highscore") {
        // Toggle: flip highScore filter
        _state[tf].highScore = !_state[tf].highScore;
        btn.classList.toggle("active", _state[tf].highScore);
      } else if (f === "ptsquare") {
        // Toggle: flip Gann P/T square filter
        _state[tf].ptSquare = !_state[tf].ptSquare;
        btn.classList.toggle("active", _state[tf].ptSquare);
      } else if (f === "gann") {
        // Toggle: flip Gann Intraday convergence filter
        _state[tf].gannIntraday = !_state[tf].gannIntraday;
        btn.classList.toggle("active", _state[tf].gannIntraday);
      } else {
        // Direction filter — deactivate siblings (not toggle-type btns)
        document.querySelectorAll(`.filter-btn[data-tf="${tf}"]:not([data-f="valid"]):not([data-f="highscore"]):not([data-f="ptsquare"]):not([data-f="gann"])`)
          .forEach(b => b.classList.remove("active", "buy-active", "sell-active"));
        if (f === "buy")       btn.classList.add("buy-active");
        else if (f === "sell") btn.classList.add("sell-active");
        else                   btn.classList.add("active");
        _state[tf].filter = f;
      }

      renderTable(tf);
    });
  });
}

// ── Run Both ─────────────────────────────────────────────────────────────────

function runBoth() {
  runScan("1d");
  runScan("15m");
}

// ── Scan lifecycle ────────────────────────────────────────────────────────────

// ── ATH universe sentinel values ─────────────────────────────────────────────
const ATH_UNIVERSES = new Set(["ath_90d", "near_ath"]);

// Update the panel subtitle to reflect current settings
function updateSubtitle(tf) {
  const el = document.getElementById("subtitle-" + tf);
  if (!el) return;
  const lb   = _lbBars || 200;
  const base = tf === "1d" ? "Swing / Positional · Pivot 12" : "Intraday · Pivot 12";
  el.textContent = `${base} · LB ${lb} bars`;
}

async function runScan(tf) {
  updateSubtitle(tf);    // reflect current LB bars in panel header before scan starts
  setRunning(tf, true);
  try {
    const resp = await fetch("/api/signals/run", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        universe:   _universe,
        timeframe:  tf,
        bos_mode:   _bosMode,
        swing_mode: _swingMode,
        lb_bars:    _lbBars,     // null = use server default (200); number = override
      }),
    });

    // ── Handle ATH-cache-miss gracefully ─────────────────────────────────────
    if (resp.status === 400) {
      let errData = {};
      try { errData = await resp.json(); } catch (_) { /* ignore */ }
      setRunning(tf, false);
      if (errData.no_ath_cache) {
        toast(
          "⚠ No ATH data yet — open the ATH Screener, run a scan, then come back.",
          "err"
        );
        // Open the screener in a new tab if the user is trying ATH universes
        if (ATH_UNIVERSES.has(_universe)) {
          setTimeout(() => {
            if (confirm("Open ATH Screener now to run the scan?")) {
              window.open("/screener", "_blank");
            }
          }, 400);
        }
      } else {
        toast("Scan error: " + (errData.error || "Bad request"), "err");
      }
      return;
    }

    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const { job_id } = await resp.json();
    pollStatus(tf, job_id);
  } catch (err) {
    setRunning(tf, false);
    toast("Scan failed [" + tf + "]: " + err.message, "err");
  }
}

function pollStatus(tf, jobId) {
  clearTimeout(_state[tf].pollTimer);
  _state[tf].pollTimer = setTimeout(async () => {
    try {
      const resp = await fetch(`/api/signals/status/${jobId}`);
      const data = await resp.json();

      if (data.status === "running" || data.status === "pending") {
        updateProgress(tf, data.progress, data.total);
        pollStatus(tf, jobId);   // keep polling
        return;
      }

      setRunning(tf, false);

      if (data.status === "done" && data.results) {
        renderResults(tf, data.results);
      } else {
        toast("Scan returned no results [" + tf + "]", "err");
      }
    } catch (err) {
      setRunning(tf, false);
      toast("Poll error [" + tf + "]: " + err.message, "err");
    }
  }, 800);
}

// ── UI state helpers ──────────────────────────────────────────────────────────

function setRunning(tf, running) {
  const btn     = document.getElementById(`runBtn-${tf}`);
  const progWrap = document.getElementById(`progress-${tf}`);

  if (!btn) return;

  btn.disabled = running;
  if (tf === "15m") {
    btn.textContent = running ? "⏳ Scanning…" : "▶ Run 15-min";
  } else {
    btn.textContent = running ? "⏳ Scanning…" : "▶ Run Daily";
  }

  if (progWrap) progWrap.classList.toggle("show", running);
  if (!running) updateProgress(tf, 0, 0);
}

function updateProgress(tf, done, total) {
  const fill  = document.getElementById(`fill-${tf}`);
  const label = document.getElementById(`label-${tf}`);
  const pct   = total > 0 ? Math.round(done / total * 100) : 0;
  if (fill)  fill.style.width    = pct + "%";
  if (label) label.textContent   = total > 0 ? `${done} / ${total}` : "";
}

// ── Render results ────────────────────────────────────────────────────────────

function renderResults(tf, results) {
  const st = _state[tf];

  // Build flat signal rows
  st.rows = [];
  const all = results.all ?? [];

  all.forEach(row => {
    if (row.current_signal) {
      st.rows.push({ ...row, _sig: row.current_signal });
    } else if (row.recent_signals && row.recent_signals.length) {
      const sig = row.recent_signals[row.recent_signals.length - 1];
      st.rows.push({ ...row, _sig: sig });
    }
  });

  // Mini cards
  const cid = cardId(tf);
  const cardsEl = document.getElementById(`cards-${tf}`);
  if (cardsEl) cardsEl.style.display = "flex";

  _setEl(`${cid}-total`, all.filter(r => r.fib_levels).length);
  _setEl(`${cid}-buy`,   (results.buy  ?? []).length);
  _setEl(`${cid}-sell`,  (results.sell ?? []).length);

  const ts = results.scanned_at
    ? new Date(results.scanned_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })
    : "—";
  _setEl(`${cid}-time`, ts);

  // Show filter row
  const filterEl = document.getElementById(`filter-${tf}`);
  if (filterEl) filterEl.style.display = "flex";

  // Signal count badge
  const sigCountEl = document.getElementById(`count-${tf}`);
  const totalSig = (results.buy ?? []).length + (results.sell ?? []).length;
  if (sigCountEl) sigCountEl.textContent = totalSig;

  renderTable(tf);
}

function _setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ── Table render ──────────────────────────────────────────────────────────────

function renderTable(tf) {
  const wrap = document.getElementById(`table-${tf}`);
  if (!wrap) return;

  const st   = _state[tf];
  let   rows = st.rows;

  // Apply direction filter
  if (st.filter === "buy")  rows = rows.filter(r => r._sig.type === "BUY");
  if (st.filter === "sell") rows = rows.filter(r => r._sig.type === "SELL");

  // Apply "confirmed only" filter:
  //   exclude ✘ false breakouts (valid===false)
  //   exclude ⚠ stale signals  (price_still_valid===false)
  //   keep    ⏳ pending         (valid===null — live bar, user decides)
  if (st.validOnly) {
    rows = rows.filter(r => {
      const sig = r._sig;
      if (sig.valid === false) return false;            // ✘ false breakout
      if (sig.price_still_valid === false) return false; // ⚠ stale
      return true;
    });
  }

  // Apply "high score" filter — 15m only (score ≥ 55)
  if (st.highScore) {
    rows = rows.filter(r => {
      const sc = (r.quality || {}).score;
      return sc != null && sc >= 55;
    });
  }

  // Apply Gann P/T square filter — stocks approaching price/time convergence (score ≥ 30)
  if (st.ptSquare) {
    rows = rows.filter(r => {
      const sc = (r.pt_square || {}).score;
      return sc != null && sc >= 30;
    });
  }

  // Apply Gann Intraday filter — stocks in today's Gann time+price convergence zone (score ≥ 45)
  if (st.gannIntraday) {
    rows = rows.filter(r => {
      const sc = (r.gann_intraday || {}).score;
      return sc != null && sc >= 45;
    });
  }

  // Apply sort
  rows = [...rows].sort(makeSorter(st.sort));

  // Update count badge
  const sigCountEl = document.getElementById(`count-${tf}`);
  if (sigCountEl && st.rows.length) sigCountEl.textContent = rows.length;

  if (!rows.length) {
    const noun = st.filter === "all" ? "" : st.filter.toUpperCase() + " ";
    wrap.innerHTML = `<div class="empty-state">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="1.5">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
      </svg>
      No ${noun}signals found in this universe
    </div>`;
    return;
  }

  const isIntraday = (tf === "15m");
  const barsLabel  = isIntraday ? "Bars" : "Days";

  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th data-col="symbol">Symbol</th>
          <th data-col="name">Company</th>
          <th data-col="_sig.type">Signal</th>
          <th data-col="_sig.valid" title="Signal bar direction + next candle close confirms">Confirm</th>
          <th data-col="current_price">LTP ₹</th>
          <th data-col="_sig.price">Sig Price ₹</th>
          <th data-col="_sig.date">Signal Date</th>
          <th data-col="_sig.bars_ago">${barsLabel} Ago</th>
          <th data-col="fib_levels.f0125">1/8 ₹</th>
          <th data-col="fib_levels.f0875">7/8 ₹</th>
          <th data-col="fib_levels.swing_low">Sw. Low ₹</th>
          <th data-col="fib_levels.swing_high">Sw. High ₹</th>
          <th data-col="day_change_pct">Day %</th>
          <th data-col="quality.score" title="${isIntraday
            ? "Quality Score = Freshness(40) + Volume Surge(30) + ATR Potential(20) + Momentum(10)"
            : "Momentum Score = Freshness(25) + Trend Align(30, EMA20 vs EMA50) + RSI Zone(25) + Volume Confirm(20, vs 20-session avg)"}">Score ⓘ</th>
          ${isIntraday ? `<th data-col="gann_intraday.score" title="Gann Intraday Convergence&#10;Time @ Gann division (40) + Price @ Sq9 (40) + Direct √P=T (20)&#10;PRIME ≥70 · SETUP ≥45&#10;Purely today's open — no historical lookback">Gann ⧖</th>` : ""}
          <th data-col="pt_square.score" title="Gann P/T Square Score&#10;Sq9 proximity (40) + Time cycle (40) + Direct √P≈T (20)&#10;SQUARE ≥60 · NEAR ≥30">P/T ⧖</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(r => signalRow(r, isIntraday)).join("")}
      </tbody>
    </table>`;

  // Sort click handlers
  wrap.querySelectorAll("thead th[data-col]").forEach(th =>
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (st.sort.col === col) st.sort.asc = !st.sort.asc;
      else { st.sort.col = col; st.sort.asc = true; }
      renderTable(tf);
    })
  );

  markSortHeader(wrap, st.sort);
}

function signalRow(r, isIntraday) {
  const sig     = r._sig;
  const isBuy   = sig.type === "BUY";
  const trClass = isBuy ? "tr-buy" : "tr-sell";
  const chgCls  = r.day_change_pct >= 0 ? "td-pos" : "td-neg";
  const pill    = isBuy
    ? `<span class="sig-pill sig-buy">▲ BUY</span>`
    : `<span class="sig-pill sig-sell">▼ SELL</span>`;

  let barsAgo;
  if (sig.bars_ago === 0)      barsAgo = isIntraday ? "This bar" : "Today";
  else if (isIntraday)         barsAgo = `${sig.bars_ago} bars`;
  else                         barsAgo = `${sig.bars_ago}d ago`;

  // ── Confirmation badge (3-layer check) ───────────────────────────────────
  //
  //  Layer 1 — next_close_ok  : next candle stayed on correct side of level
  //  Layer 2 — signal_bar_ok  : signal candle moved in signal direction
  //  Layer 3 — price_still_valid: CURRENT price still on correct side
  //
  //  ✔ green  — all three pass → strong, still-valid signal
  //  ⚠ amber  — next candle ok but price has since reversed → stale signal
  //  ✘ red    — next candle reversed immediately → false breakout, skip
  //  ⏳       — live bar, next candle not yet closed → wait
  let confirmCell;
  if (sig.valid === false) {
    // Next candle already reversed → definite false breakout
    confirmCell = `<span title="False breakout — next candle reversed back through level" style="color:var(--red);font-weight:700;font-size:13px">✘</span>`;
  } else if (sig.valid === null || sig.valid === undefined) {
    // Live / most-recent bar — still waiting for next candle to close
    confirmCell = `<span title="Awaiting next candle close — do not enter yet" style="color:var(--text-2);font-size:11px">⏳</span>`;
  } else if (sig.valid === true && sig.price_still_valid === false) {
    // Next candle was ok, but price has since bounced back through the level
    confirmCell = `<span title="Stale — price reversed past the trigger level after signal" style="color:#f39c12;font-weight:700;font-size:14px">⚠</span>`;
  } else {
    // valid=true AND price still on correct side → clean, actionable signal
    confirmCell = `<span title="Confirmed — next candle closed ok AND price still on correct side" style="color:var(--green);font-weight:700;font-size:13px">✔</span>`;
  }

  // ── Quality / momentum score badge (both panels) ──────────────────────────
  let scoreCell;
  {
    const q  = r.quality || {};
    const sc = q.score;
    if (sc == null) {
      scoreCell = `<td><span class="q-score q-na">—</span></td>`;
    } else {
      const cls = sc >= 65 ? "q-high" : sc >= 45 ? "q-mid" : "q-low";
      const tip = isIntraday
        ? `Score ${sc}/100\nFreshness: ${q.score_fresh??0}/40\nVolume: ${q.score_vol??0}/30 (${q.vol_ratio??0}×)\nATR: ${q.score_atr??0}/20 (${q.atr_pct??0}%)\nMomentum: ${q.score_momentum??0}/10`
        : `Score ${sc}/100\nFreshness: ${q.score_fresh??0}/25\nTrend: ${q.score_trend??0}/30 (EMA20/50 ${q.trend??"—"})\nRSI: ${q.score_rsi??0}/25 (RSI14 ${q.rsi??"—"})\nVolume: ${q.score_vol??0}/20 (${q.vol_ratio??0}× 20-day avg)`;
      scoreCell = `<td title="${esc(tip)}"><span class="q-score ${cls}">${sc}</span></td>`;
    }
  }

  // ── Gann Intraday Convergence badge (15m only) ────────────────────────────
  let gannCell = "";
  if (isIntraday) {
    const gi  = r.gann_intraday || {};
    const gs  = gi.score;
    if (gs == null) {
      gannCell = `<td style="text-align:center;color:var(--text-2)">—</td>`;
    } else {
      const giTip = `Gann Intraday Score ${gs}/100\n` +
        `Time turn @ ${gi.nearest_time_level ?? "—"} · gap ${gi.time_gap ?? "?"} bar(s) · ${gi.bars_since_open ?? "?"} bars since open`;
      if (gs >= 70) {
        gannCell = `<td title="${esc(giTip)}"><span class="q-score gann-prime">PRIME</span></td>`;
      } else if (gs >= 45) {
        gannCell = `<td title="${esc(giTip)}"><span class="q-score gann-setup">SETUP</span></td>`;
      } else {
        gannCell = `<td title="${esc(giTip)}" style="text-align:center;font:600 10px var(--mono);color:var(--text-2)">${gs}</td>`;
      }
    }
  }

  // ── Gann P/T Square badge (both panels) ────────────────────────────────────
  const pt  = r.pt_square || {};
  const pts = pt.score;
  let ptCell;
  if (pts == null) {
    ptCell = `<td style="text-align:center;color:var(--text-2)">—</td>`;
  } else {
    const ptTip = `Gann Time Cycle Score ${pts}/100\nNearest cycle: ${pt.nearest_cycle ?? "—"} bars · elapsed ${pt.bars_from_pivot ?? "—"} bars · gap ${pt.cycle_gap ?? "?"}`;
    if (pts >= 60) {
      ptCell = `<td title="${esc(ptTip)}"><span class="q-score pt-square">SQUARE</span></td>`;
    } else if (pts >= 30) {
      ptCell = `<td title="${esc(ptTip)}"><span class="q-score pt-near">NEAR</span></td>`;
    } else {
      // Low score — show as a plain number with tooltip; no badge box needed
      ptCell = `<td title="${esc(ptTip)}" style="text-align:center;font:600 10px var(--mono);color:var(--text-2)">${pts}</td>`;
    }
  }

  return `<tr class="${trClass}">
    <td class="td-sym">${r.symbol.replace(".NS","")}</td>
    <td class="td-name" title="${esc(r.name)}">${esc(r.name)}</td>
    <td>${pill}</td>
    <td style="text-align:center">${confirmCell}</td>
    <td class="td-price">₹${fmt(r.current_price)}</td>
    <td class="td-price">₹${fmt(sig.price)}</td>
    <td class="td-num">${sig.date}</td>
    <td class="td-num"><span class="${isBuy ? "ath-pill" : "near-pill"}">${barsAgo}</span></td>
    <td class="td-num" style="color:var(--green)">₹${fmt(r.fib_levels.f0125)}</td>
    <td class="td-num" style="color:var(--red)">₹${fmt(r.fib_levels.f0875)}</td>
    <td class="td-num td-name">₹${fmt(r.fib_levels.swing_low)}</td>
    <td class="td-num td-name">₹${fmt(r.fib_levels.swing_high)}</td>
    <td class="${chgCls}">${r.day_change_pct >= 0 ? "+" : ""}${r.day_change_pct}%</td>
    ${scoreCell}
    ${gannCell}
    ${ptCell}
  </tr>`;
}

// ── CSV export ────────────────────────────────────────────────────────────────

function exportCSV(tf) {
  const rows = _state[tf].rows;
  if (!rows.length) { toast("No data to export", "err"); return; }

  const headers = [
    "Symbol","Company","Signal","LTP","Signal Price","Signal Date",
    "Bars Ago","1/8 Level","7/8 Level","Swing Low","Swing High","Day %","Momentum Score"
  ];

  const csvRows = rows.map(r => [
    r.symbol.replace(".NS",""), r.name, r._sig.type,
    r.current_price, r._sig.price, r._sig.date, r._sig.bars_ago,
    r.fib_levels.f0125, r.fib_levels.f0875,
    r.fib_levels.swing_low, r.fib_levels.swing_high,
    r.day_change_pct, (r.quality || {}).score ?? "",
  ].map(v => JSON.stringify(v ?? "")).join(","));

  const csv  = [headers.join(","), ...csvRows].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  const date = new Date().toISOString().slice(0, 10);
  a.href     = url;
  a.download = `swing_fib_${tf}_${date}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── TradingView status badge ──────────────────────────────────────────────────

async function refreshTvStatus() {
  try {
    const resp = await fetch("/api/tv/status");
    const info = await resp.json();
    const badge = document.getElementById("tvBadge");
    const txt   = document.getElementById("tvBadgeText");
    if (!badge || !txt) return;
    if (info.connected) {
      badge.className  = "tv-badge connected";
      txt.textContent  = "TV Desktop ✓";
    } else {
      badge.className  = "tv-badge disconnected";
      txt.textContent  = "TV Desktop ✗";
    }
  } catch { /* silent */ }
}

// ── Sort utilities ────────────────────────────────────────────────────────────

function makeSorter({ col, asc }) {
  return (a, b) => {
    const get = (obj, path) => path.split(".").reduce((o, k) => (o ?? {})[k], obj);
    const av  = get(a, col);
    const bv  = get(b, col);
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

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, type = "ok") {
  const area = document.getElementById("toastArea");
  if (!area) return;
  const el = document.createElement("div");
  el.className  = `toast ${type}`;
  el.textContent = msg;
  area.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, 3500);
}

// ═══════════════════════════════════════════════════════════════════════════════
// ── Auto-poll: 15-min intraday refresh every 15 minutes ──────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

function toggleAutoPoll15m() {
  _autoPollOn = !_autoPollOn;
  const btn       = document.getElementById("autoPollBtn");
  const cntEl     = document.getElementById("autoPollCountdown");

  if (_autoPollOn) {
    // ── Enable ────────────────────────────────────────────────────────────────
    btn.classList.add("on");
    btn.title = "Auto-refresh ON — click to stop";
    cntEl.style.display = "inline";
    toast("⏱ Auto-refresh ON — scanning every 15 min with current settings", "ok");

    // Run immediately, then schedule repeating every 15 min
    runScan("15m");
    _scheduleNextAutoPoll();

  } else {
    // ── Disable ───────────────────────────────────────────────────────────────
    _stopAutoPoll();
    toast("⏱ Auto-refresh stopped", "ok");
  }
}

function _scheduleNextAutoPoll() {
  clearInterval(_autoPollTimer);
  clearInterval(_autoPollCountdown);

  _autoPollNextAt = Date.now() + AUTO_POLL_INTERVAL_MS;

  // Countdown tick — updates every second
  _autoPollCountdown = setInterval(() => {
    const remaining = Math.max(0, _autoPollNextAt - Date.now());
    const m = Math.floor(remaining / 60000);
    const s = Math.floor((remaining % 60000) / 1000);
    const el = document.getElementById("autoPollCountdown");
    if (el) el.textContent = `Next: ${m}:${String(s).padStart(2, "0")}`;
  }, 1000);

  // Actual scan trigger at interval end
  _autoPollTimer = setInterval(() => {
    if (!_autoPollOn) { _stopAutoPoll(); return; }
    runScan("15m");
    // Reset countdown for next cycle
    _autoPollNextAt = Date.now() + AUTO_POLL_INTERVAL_MS;
  }, AUTO_POLL_INTERVAL_MS);
}

function _stopAutoPoll() {
  _autoPollOn = false;
  clearInterval(_autoPollTimer);
  clearInterval(_autoPollCountdown);
  _autoPollTimer     = null;
  _autoPollCountdown = null;
  _autoPollNextAt    = 0;

  const btn   = document.getElementById("autoPollBtn");
  const cntEl = document.getElementById("autoPollCountdown");
  if (btn)   { btn.classList.remove("on"); btn.title = "Auto-refresh 15-min signals every 15 minutes"; }
  if (cntEl) { cntEl.style.display = "none"; cntEl.textContent = ""; }
}

// Stop auto-poll if user navigates away
window.addEventListener("beforeunload", _stopAutoPoll);
