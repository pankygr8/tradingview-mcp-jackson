"""
eod.py — end-of-day scorecard, date-wise dashboard, and screener-tuning hints.

  python -m scanner_agent eod            (also runs automatically after the
                                          evening report in `cycle`)

What it produces (scanner_agent/reports/)
  daily.html          date-wise dashboard: per day → trades, success, failed,
                      success %, R; per-scanner breakdown and every trade
  daily_stats.csv     one row per date × scanner × bucket
  trades_by_day.csv   every graded trade with its outcome
  tuning.csv / tuning.md
                      for each scanner bucket: which measurable condition
                      (rvol, score, R:R, side, time of day …) separates winning
                      trades from losing ones — the evidence for changing a
                      screener's filters

Definitions
  trade     a signal that was actually entered (trigger entries that never
            fill are "no fill", plans with stop/target on the wrong side are
            "bad plan" — neither counts as a trade)
  plan      the scanner's OWN stop/target when it has one, otherwise the
            uniform plan (daily: 1.5×ATR stop / 3×ATR target / 10 bars;
            intraday: 0.25×day-range stop / 2R target / exit at close)
  success   trade closed with R > 0  (target hit, or time exit in profit)
  failed    trade closed with R <= 0
  open      daily trade still running (not counted until it closes)

Nothing here changes a screener. It produces the evidence; the decision to
change a threshold is made by you (or the scanner-strategist agent) once a
bucket has enough closed trades for the difference to be more than noise.
"""

from __future__ import annotations

import html
import json
import math
import os

import numpy as np
import pandas as pd

from . import config, report
from .adapters import SCANNER_LABELS, ist_now

CLOSED = ("win", "loss", "time_exit")
SKIP_FEATURES = {"plan_entry", "f0125", "f0875", "nearest_level", "half_ath_price",
                 "all_time_high", "bars_ago"}


# ═════════════════════════════════════════════════════════════════════════════
# Per-trade table
# ═════════════════════════════════════════════════════════════════════════════

def trades_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One row per signal with a single resolved outcome (native plan if the
    scanner has one, else the uniform plan)."""
    if df.empty:
        return df
    t = df.copy()
    for c in ("native_outcome", "native_r", "uni_outcome", "uni_r", "native_bars", "uni_bars"):
        if c not in t:
            t[c] = np.nan
    has_native = t["native_outcome"].notna()
    t["plan"] = np.where(t["stop"].notna(), "native", "uniform")
    t["outcome"] = np.where(has_native, t["native_outcome"], t["uni_outcome"])
    t["R"] = pd.to_numeric(np.where(has_native, t["native_r"], t["uni_r"]), errors="coerce")
    t["bars_held"] = np.where(has_native, t["native_bars"], t["uni_bars"])
    t["outcome"] = t["outcome"].fillna(t["o_status"].map(
        {"no_data": "no_data", "pending": "pending"}).fillna("pending"))
    t["closed"] = t["outcome"].isin(CLOSED)
    t["result"] = np.select(
        [t["closed"] & (t["R"] > 0), t["closed"] & (t["R"] <= 0),
         t["outcome"].eq("no_fill"), t["outcome"].eq("invalid"), t["outcome"].eq("open")],
        ["success", "failed", "no_fill", "bad_plan", "open"], default="pending")
    # signal hour (IST) — the intraday clock matters
    ts = pd.to_datetime(t["signal_ts"], errors="coerce", utc=True)
    t["signal_hour"] = ts.dt.tz_convert("Asia/Kolkata").dt.hour
    # flatten the scanner's own metrics (rvol, rr, coil_ratio …) into columns
    metas = t["meta"].apply(lambda s: json.loads(s) if isinstance(s, str) and s else {})
    feat = pd.json_normalize(metas.tolist()).add_prefix("f_")
    feat.index = t.index
    return pd.concat([t, feat], axis=1)


# ═════════════════════════════════════════════════════════════════════════════
# Date-wise stats
# ═════════════════════════════════════════════════════════════════════════════

def _agg(g: pd.DataFrame) -> dict:
    closed = g[g["closed"]]
    rs = closed["R"].dropna()
    n = len(closed)
    s = int((g["result"] == "success").sum())
    return {
        "signals": len(g),
        "trades": n,
        "success": s,
        "failed": int((g["result"] == "failed").sum()),
        "success%": round(s / n * 100, 1) if n else None,
        "total_R": round(float(rs.sum()), 2) if n else 0.0,
        "avg_R": round(float(rs.mean()), 3) if n else None,
        "open": int((g["result"] == "open").sum()),
        "no_fill": int((g["result"] == "no_fill").sum()),
        "bad_plan": int((g["result"] == "bad_plan").sum()),
        "pending": int((g["result"] == "pending").sum()),
    }


def daily_stats(tr: pd.DataFrame, source: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    t = tr[tr["source"] == source]
    if t.empty:
        return pd.DataFrame(), pd.DataFrame()
    by_day = pd.DataFrame([{"session": d, **_agg(g)} for d, g in t.groupby("session")])
    by_day = by_day.sort_values("session", ascending=False).reset_index(drop=True)
    by_scn = pd.DataFrame([{"session": d, "scanner": s, "bucket": b, **_agg(g)}
                           for (d, s, b), g in t.groupby(["session", "scanner", "bucket"])])
    return by_day, by_scn


# ═════════════════════════════════════════════════════════════════════════════
# Tuning: which conditions separate winners from losers
# ═════════════════════════════════════════════════════════════════════════════

def tuning(tr: pd.DataFrame, min_trades: int = 30, min_bin: int = 10) -> pd.DataFrame:
    closed = tr[tr["closed"] & tr["R"].notna()]
    rows = []
    for (scn, bkt), g in closed.groupby(["scanner", "bucket"]):
        if len(g) < min_trades:
            continue
        base_win = (g["R"] > 0).mean() * 100
        base_r = g["R"].mean()
        feats = ["score", "side", "signal_hour"] + [c for c in g.columns if c.startswith("f_")
                                                    and c[2:] not in SKIP_FEATURES]
        for f in feats:
            col = g[f]
            if col.notna().mean() < 0.7:
                continue
            sub = g[col.notna()]
            vals = sub[f]
            # categorical / boolean
            if vals.dtype == object or vals.dtype == bool or vals.nunique() <= 3:
                for v, h in sub.groupby(f):
                    if len(h) < min_bin:
                        continue
                    rows.append(_tune_row(scn, bkt, f, f"= {v}", h, base_win, base_r, len(g)))
                continue
            try:
                bins = pd.qcut(vals.astype(float), 3, duplicates="drop")
            except (ValueError, TypeError):
                continue
            cats = list(bins.cat.categories)
            for k, (iv, h) in enumerate(sub.groupby(bins, observed=True)):
                if len(h) < min_bin:
                    continue
                if k == 0:
                    rule = f"<= {iv.right:.4g}"
                elif k == len(cats) - 1:
                    rule = f">= {iv.left:.4g}"
                else:
                    rule = f"{iv.left:.4g} – {iv.right:.4g}"
                rows.append(_tune_row(scn, bkt, f, rule, h, base_win, base_r, len(g)))
    if not rows:
        return pd.DataFrame()
    tu = pd.DataFrame(rows)
    tu["lift_R"] = (tu["avg_R"] - tu["base_avg_R"]).round(3)
    tu["lift_win%"] = (tu["win%"] - tu["base_win%"]).round(1)
    # a slice only matters if it is both better AND not a tiny corner
    tu["suggest"] = np.where((tu["lift_R"] >= 0.15) & (tu["t_vs_rest"] >= 1.5), "KEEP only this",
                    np.where((tu["lift_R"] <= -0.15) & (tu["t_vs_rest"] <= -1.5), "FILTER OUT", ""))
    return tu.sort_values(["scanner", "bucket", "lift_R"], ascending=[True, True, False]).reset_index(drop=True)


def _tune_row(scn, bkt, f, rule, h, base_win, base_r, n_all):
    feature = f[2:] if f.startswith("f_") else f
    return {"scanner": scn, "bucket": bkt, "feature": feature, "condition": f"{feature} {rule}",
            "trades": len(h), "share%": round(len(h) / n_all * 100, 1),
            "win%": round((h["R"] > 0).mean() * 100, 1), "avg_R": round(h["R"].mean(), 3),
            "base_win%": round(base_win, 1), "base_avg_R": round(base_r, 3),
            "live_trades": int((h["source"] == "live").sum()),
            "t_vs_rest": _tstat(h["R"])}


def _tstat(a: pd.Series) -> float:
    """t-statistic of the slice's mean R against zero."""
    a = a.dropna()
    if len(a) < 3 or a.std() == 0:
        return 0.0
    return round(float(a.mean() / (a.std() / math.sqrt(len(a)))), 2)


# ═════════════════════════════════════════════════════════════════════════════
# HTML
# ═════════════════════════════════════════════════════════════════════════════

EXTRA_CSS = """
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:8px 0 4px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mute);font-size:12px}
nav a{color:var(--acc);text-decoration:none;margin-right:14px;font-weight:600}
details{border-bottom:1px solid var(--line)}details:last-child{border-bottom:0}
summary{cursor:pointer;list-style:none;padding:0}summary::-webkit-details-marker{display:none}
summary table td{border-bottom:0}.inner{padding:4px 12px 14px;background:var(--bg)}
.inner table{background:var(--card);border:1px solid var(--line);border-radius:8px;margin-top:8px}
.meter{display:inline-block;width:70px;height:8px;border-radius:4px;background:var(--line);vertical-align:middle;margin-left:6px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--pos)}
svg text{fill:var(--mute);font-size:11px}
table.fx{table-layout:fixed;min-width:980px}table.fx td:first-child,table.fx th:first-child{width:130px}
"""


def _day_row_cells(r) -> str:
    sp = r["success%"]
    meter = f"<span class='meter'><i style='width:{sp or 0}%'></i></span>" if sp is not None and not pd.isna(sp) else ""
    return "".join([
        f"<td>{r['signals']}</td><td><b>{r['trades']}</b></td>",
        f"<td class='pos'>{r['success']}</td><td class='neg'>{r['failed']}</td>",
        f"<td>{report._fmt(sp)}{meter}</td>",
        f"<td class='{report._cls(r['avg_R'])}'>{report._fmt(r['avg_R'], True)}</td>",
        f"<td class='{report._cls(r['total_R'])}'>{report._fmt(float(r['total_R']), True)}</td>",
        f"<td>{r['open']}</td><td>{r['no_fill']}</td><td>{r['bad_plan']}</td>",
    ])


HEAD = ("<th>Signals</th><th>Trades</th><th>Success</th><th>Failed</th><th>Success %</th>"
        "<th>Avg R</th><th>Total R</th><th>Open</th><th>No fill</th><th>Bad plan</th>")


def _chart(by_day: pd.DataFrame) -> str:
    if by_day.empty:
        return ""
    d = by_day.sort_values("session").tail(60)
    if d.empty or d["trades"].sum() == 0:
        return ""
    W, H, pad = 900, 170, 28
    vals = d["total_R"].astype(float).tolist()
    cum = np.cumsum(vals)
    mx = max(max(abs(v) for v in vals), 1e-6)
    cmx = max(abs(cum).max(), 1e-6)
    n = len(vals)
    bw = (W - 2 * pad) / n
    mid = H / 2
    bars = []
    for i, (v, s) in enumerate(zip(vals, d["session"])):
        h = abs(v) / mx * (H / 2 - 14)
        y = mid - h if v >= 0 else mid
        bars.append(f"<rect x='{pad + i * bw + 1:.1f}' y='{y:.1f}' width='{max(bw - 2, 1):.1f}' height='{h:.1f}' "
                    f"rx='2' fill='var(--{'pos' if v >= 0 else 'neg'})'><title>{s}: {v:+.2f}R</title></rect>")
    pts = " ".join(f"{pad + i * bw + bw / 2:.1f},{mid - c / cmx * (H / 2 - 14):.1f}" for i, c in enumerate(cum))
    return (f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' aria-label='Daily total R and cumulative R'>"
            f"<line x1='{pad}' x2='{W - pad}' y1='{mid}' y2='{mid}' stroke='var(--line)'/>{''.join(bars)}"
            f"<polyline points='{pts}' fill='none' stroke='var(--acc)' stroke-width='2'/>"
            f"<text x='{pad}' y='12'>bars = total R per day · line = cumulative R ({cum[-1]:+.1f}R)</text>"
            f"<text x='{pad}' y='{H - 4}'>{d['session'].iloc[0]}</text>"
            f"<text x='{W - pad}' y='{H - 4}' text-anchor='end'>{d['session'].iloc[-1]}</text></svg>")


def _card(inner: str) -> str:
    return f"<div class='card' style='padding:8px 12px;margin-bottom:10px'>{inner}</div>" if inner else ""


def _trade_table(t: pd.DataFrame) -> str:
    cols = ["Time", "Symbol", "Scanner", "Bucket", "Side", "Plan", "Entry", "Stop", "Target", "Fill", "Exit", "Result", "R"]
    out = ["<table><tr>" + "".join(f"<th class='{'l' if i in (1, 2, 3, 4, 5, 11) else ''}'>{c}</th>" for i, c in enumerate(cols)) + "</tr>"]
    order = {"success": 0, "failed": 1, "open": 2, "no_fill": 3, "bad_plan": 4, "pending": 5}
    t = t.assign(_o=t["result"].map(order)).sort_values(["_o", "R"], ascending=[True, False])
    for _, r in t.iterrows():
        pre = "native_" if r["plan"] == "native" else "uni_"
        res = r["result"]
        out.append("<tr>" + "".join([
            f"<td>{str(r['signal_ts'])[11:16]}</td>",
            f"<td class='l'><b>{html.escape(str(r['symbol']).replace('.NS', ''))}</b></td>",
            f"<td class='l'>{html.escape(SCANNER_LABELS.get(r['scanner'], r['scanner']).split(' (')[0])}</td>",
            f"<td class='l'><span class='chip'>{html.escape(r['bucket'])}</span></td>",
            f"<td class='l {'pos' if r['side'] == 'long' else 'neg'}'>{r['side']}</td>",
            f"<td class='l'>{r['plan']}</td>",
            f"<td>{report._fmt(r.get('entry'))}</td><td>{report._fmt(r.get('stop'))}</td><td>{report._fmt(r.get('tp1'))}</td>",
            f"<td>{report._fmt(r.get(pre + 'fill'))}</td><td>{report._fmt(r.get(pre + 'exit'))}</td>",
            f"<td class='l {'pos' if res == 'success' else 'neg' if res == 'failed' else ''}'>{res.replace('_', ' ')}</td>",
            f"<td class='{report._cls(r['R'])}'>{report._fmt(r['R'], True)}</td>",
        ]) + "</tr>")
    return "".join(out) + "</table>"


def _day_sections(tr, by_day, by_scn, source) -> str:
    if by_day.empty:
        return "<p class='note'>No signals for this source yet.</p>"
    t = tr[tr["source"] == source]
    parts = ["<table class='fx'><tr><th class='l'>Date</th>" + HEAD + "</tr></table>"]
    for _, r in by_day.iterrows():
        d = r["session"]
        sc = by_scn[by_scn["session"] == d].sort_values("total_R", ascending=False)
        sc_rows = "".join(
            f"<tr><td class='l'>{html.escape(SCANNER_LABELS.get(x['scanner'], x['scanner']).split(' (')[0])}</td>"
            f"<td class='l'><span class='chip'>{html.escape(x['bucket'])}</span></td>{_day_row_cells(x)}</tr>"
            for _, x in sc.iterrows())
        trades = t[t["session"] == d]
        show_trades = source == "live" or len(trades) <= 60
        parts.append(
            f"<details><summary><table class='fx'><tr><td class='l'><b>{d}</b> ▸</td>{_day_row_cells(r)}</tr></table></summary>"
            f"<div class='inner'><table><tr><th class='l'>Scanner</th><th class='l'>Bucket</th>{HEAD}</tr>{sc_rows}</table>"
            + (_trade_table(trades) if show_trades else f"<p class='note'>{len(trades)} trades — see trades_by_day.csv</p>")
            + "</div></details>")
    return "".join(parts)


def _tuning_table(tu: pd.DataFrame, cfg) -> str:
    if tu.empty:
        return (f"<p class='note'>Not enough closed trades yet. A bucket needs ≥ {cfg.get('tuning_min_trades', 30)} "
                "closed trades before its winners and losers can be compared.</p>")
    flagged = tu[tu["suggest"] != ""]
    show = flagged if not flagged.empty else tu.sort_values("lift_R", ascending=False).head(15)
    head = ["Scanner", "Bucket", "Condition", "Trades", "Share", "Win %", "Bucket win %", "Avg R", "Bucket avg R", "Lift R", "t", "Live n", "Suggestion"]
    out = ["<table><tr>" + "".join(f"<th class='{'l' if i < 3 or i == 12 else ''}'>{h}</th>" for i, h in enumerate(head)) + "</tr>"]
    for _, r in show.iterrows():
        out.append("<tr>" + "".join([
            f"<td class='l'>{html.escape(SCANNER_LABELS.get(r['scanner'], r['scanner']).split(' (')[0])}</td>",
            f"<td class='l'><span class='chip'>{html.escape(r['bucket'])}</span></td>",
            f"<td class='l'><code>{html.escape(r['condition'])}</code></td>",
            f"<td>{r['trades']}</td><td>{r['share%']}%</td><td>{r['win%']}</td><td>{r['base_win%']}</td>",
            f"<td class='{report._cls(r['avg_R'])}'>{report._fmt(float(r['avg_R']), True)}</td>",
            f"<td>{report._fmt(float(r['base_avg_R']), True)}</td>",
            f"<td class='{report._cls(r['lift_R'])}'>{report._fmt(float(r['lift_R']), True)}</td>",
            f"<td>{r['t_vs_rest']}</td><td>{r['live_trades']}</td>",
            f"<td class='l'><b class='{'pos' if r['suggest'].startswith('KEEP') else 'neg'}'>{r['suggest']}</b></td>",
        ]) + "</tr>")
    note = "" if not flagged.empty else "<p class='note'>No condition is strong enough to act on yet — showing the largest differences so far.</p>"
    return note + "".join(out) + "</table>"


def render(cfg: dict | None = None, log=print) -> dict:
    cfg = cfg or config.load()
    df = report.load_frame()
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    if df.empty:
        log("eod: no signals yet")
        return {}
    tr = trades_frame(df)
    live_day, live_scn = daily_stats(tr, "live")
    rep_day, rep_scn = daily_stats(tr, "replay")
    tu = tuning(tr, cfg.get("tuning_min_trades", 30), cfg.get("tuning_min_bin", 10))

    # data files
    keep = ["session", "signal_ts", "source", "scanner", "bucket", "symbol", "side", "plan", "entry", "stop", "tp1",
            "native_fill", "native_exit", "uni_fill", "uni_exit", "outcome", "result", "R", "bars_held", "score"]
    tr[[c for c in keep if c in tr]].to_csv(os.path.join(config.REPORT_DIR, "trades_by_day.csv"), index=False)
    pd.concat([live_scn.assign(source="live"), rep_scn.assign(source="replay")]).to_csv(
        os.path.join(config.REPORT_DIR, "daily_stats.csv"), index=False)
    if not tu.empty:
        tu.to_csv(os.path.join(config.REPORT_DIR, "tuning.csv"), index=False)

    # KPIs: latest live day, and all live days
    def kpis(row, label):
        if row is None:
            return ""
        sp = row["success%"]
        return (f"<h2>{label}</h2><div class='kpis'>"
                f"<div class='kpi'><b>{row['trades']}</b><span>trades closed</span></div>"
                f"<div class='kpi'><b class='pos'>{row['success']}</b><span>success</span></div>"
                f"<div class='kpi'><b class='neg'>{row['failed']}</b><span>failed</span></div>"
                f"<div class='kpi'><b>{report._fmt(sp)}{'%' if sp is not None and not pd.isna(sp) else ''}</b><span>success ratio</span></div>"
                f"<div class='kpi'><b class='{report._cls(row['total_R'])}'>{report._fmt(float(row['total_R']), True)}R</b><span>total R</span></div>"
                f"<div class='kpi'><b>{row['open']}</b><span>still open</span></div></div>")

    latest = live_day.iloc[0] if not live_day.empty else None
    all_live = None
    if not live_day.empty:
        t_live = tr[tr["source"] == "live"]
        all_live = _agg(t_live)

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Daily Scanner Results</title><style>{report.CSS}{EXTRA_CSS}</style></head><body><main>
<nav><a href="index.html">Leaderboard</a><a href="daily.html">Daily results</a><a href="crypto/index.html">Crypto</a><a href="commodity/index.html">Commodities</a></nav>
<h1>Daily scanner results</h1>
<p class="sub">Updated {ist_now():%d %b %Y %H:%M} IST · success = trade closed with R &gt; 0 · page reloads every 5 min</p>
{kpis(latest, f"Latest session — {latest['session']}" if latest is not None else "")}
{kpis(all_live, "All live sessions") if all_live is not None else "<p class='note'>No live signals yet.</p>"}
<h2>Live — by date</h2>{_card(_chart(live_day))}
<div class="card">{_day_sections(tr, live_day, live_scn, "live")}</div>
<h2>What separates winners from losers — evidence for screener changes</h2>
<p class="note">Each bucket's closed trades (live + replay) are split by a condition the screener already measures. <b>KEEP only this</b> =
that slice earns ≥ 0.15R more per trade than the bucket and is statistically positive; <b>FILTER OUT</b> = the reverse. Treat a suggestion as
real only when it holds in live trades too (Live n) and survives another few weeks of data.</p>
<div class="card">{_tuning_table(tu, cfg)}</div>
<h2>Historical replay — by date</h2><p class="note">Daily scanners re-run on past dates. Survivorship bias applies.</p>
{_card(_chart(rep_day))}<div class="card">{_day_sections(tr, rep_day, rep_scn, "replay")}</div>
</main></body></html>"""
    path = os.path.join(config.REPORT_DIR, "daily.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    _md(live_day, live_scn, tu)
    log(f"eod: {path}  ({len(live_day)} live days, {len(rep_day)} replay days, {0 if tu.empty else int((tu['suggest'] != '').sum())} tuning flags)")
    return {"html": path}


def _md(live_day, live_scn, tu):
    lines = [f"# EOD scorecard — {ist_now():%Y-%m-%d %H:%M} IST", ""]
    if not live_day.empty:
        lines += ["## Live results by date", "", live_day.head(30).to_markdown(index=False), ""]
        last = live_day.iloc[0]["session"]
        lines += [f"## {last} by scanner", "",
                  live_scn[live_scn["session"] == last].to_markdown(index=False), ""]
    if not tu.empty:
        fl = tu[tu["suggest"] != ""]
        lines += ["## Tuning flags (conditions that separate winners from losers)", "",
                  (fl if not fl.empty else tu.head(15)).to_markdown(index=False)]
    with open(os.path.join(config.REPORT_DIR, "eod.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
