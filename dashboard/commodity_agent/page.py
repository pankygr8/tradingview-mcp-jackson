"""page.py — the Commodities page (commodity/index.html on the dashboard)."""

from __future__ import annotations

import html
import json
import os

import pandas as pd

from crypto_agent.live import CSS, _c, _f, _ist, _table

from . import config, engine

IST = "Asia/Kolkata"


def _inr(v, fx):
    try:
        return f"≈ ₹{float(v) * fx:,.0f}" if fx else ""
    except (TypeError, ValueError):
        return ""


def _live_section(cfg) -> str:
    if not os.path.isfile(engine.STATUS):
        return "<p class='note'>Live tracking starts after the first backtest run.</p>"
    with open(engine.STATUS) as f:
        st = json.load(f)
    fx = st.get("usdinr")
    start = pd.Timestamp(st["start"]).tz_convert(IST)
    rows = []
    for s in st["status"]:
        unit = cfg["instruments"][s["instrument"]]["unit"]
        p = s["position"]
        if p:
            pos = (f"<b class='{'pos' if p['side'] == 'long' else 'neg'}'>{p['side'].upper()}</b> @ {_f(p['entry'])} "
                   f"<span class='note'>{_inr(p['entry'], fx)}</span><br><span class='note'>stop {_f(p['stop'])} · target "
                   f"{_f(p['target'])} · time stop {_ist(p['time_stop'], True)}</span>")
            pnl = f"<span class='{_c(p['unreal_R'])}'>{_f(p['unreal_R'], 2, True)}R</span>"
        else:
            pos, pnl = "flat — waiting for signal", "–"
        watch = (f"{s['watch_basis']} &gt; {_f(s['watch_hi'])} <span class='note'>{_inr(s['watch_hi'], fx)}</span> → long<br>"
                 f"{s['watch_basis']} &lt; {_f(s['watch_lo'])} <span class='note'>{_inr(s['watch_lo'], fx)}</span> → short"
                 if s.get("watch_hi") else "–")
        winp = f"{s['wins'] / s['closed'] * 100:.0f}%" if s["closed"] else "–"
        rows.append(
            f"<tr><td class='l'><b>{s['instrument']}</b><br><span class='note'>{unit}</span></td>"
            f"<td class='l'>{html.escape(s['strategy'])}<br><span class='note'>target {s['target_atr']:g}×ATR · stop "
            f"{s['stop_atr']:g}×ATR · ATR {_f(s.get('atr_pct'), 2)}%</span></td><td class='l'>{pos}</td><td>{pnl}</td>"
            f"<td class='l'>{watch}</td><td>{s['closed']}</td><td>{winp}</td><td class='{_c(s['total_R'])}'>{_f(s['total_R'], 2, True)}</td>"
            f"<td>{_f(s['exp_R'], 2, True)}<br><span class='note'>{html.escape(s['verdict'])}</span></td></tr>")
    closed = ""
    if os.path.isfile(engine.TRADES) and os.path.getsize(engine.TRADES) > 5:
        t = pd.read_csv(engine.TRADES)
        t = t[t.status == "closed"].sort_values("exit_ts", ascending=False) if not t.empty else t
        if not t.empty:
            closed = ("<h3 style='font-size:14px;margin:16px 0 6px'>Closed trades</h3><div class='card'>" + _table(
                ["Instrument", "Strategy", "Side", "Entry (IST)", "Entry", "Exit (IST)", "Exit", "Result", "R"],
                [f"<tr><td class='l'>{r.instrument}</td><td class='l'>{r.strategy}</td><td class='l'>{r.side}</td>"
                 f"<td>{_ist(r.entry_ts, True)}</td><td>{_f(r.entry)}</td><td>{_ist(r.exit_ts, True)}</td><td>{_f(r.exit)}</td>"
                 f"<td class='l'>{str(r.outcome).replace('_', ' ')}</td><td class='{_c(r.R)}'>{_f(r.R, 2, True)}</td></tr>"
                 for _, r in t.iterrows()], 3) + "</div>")
    return (f"<h2>Paper trading — live since {start:%d %b %Y %H:%M} IST</h2>"
            "<p class='note'>Each instrument × strategy uses the ATR target/stop that did best in the weaker half of its "
            "10-year test. Signals on CLOSED daily bars (hourly for bos_pullback), entry at the next hour's open. ₹ values = "
            "US price × USD/INR, a rough guide to the MCX level (MCX trades at a small premium/discount).</p>"
            "<div class='card'>" + _table(["Instrument", "Strategy", "Position", "Open P&L", "Next signal if…", "Closed",
                                           "Win %", "Total R", "Backtest R/trade"], rows, 5) + "</div>" + closed)


def _backtest_section() -> str:
    p = os.path.join(config.REPORT_DIR, "commodity_backtest.csv")
    if not os.path.isfile(p):
        return ""
    res = pd.read_csv(p)
    tp = os.path.join(config.REPORT_DIR, "commodity_trades.csv.gz")
    trades = pd.read_csv(tp) if os.path.isfile(tp) else pd.DataFrame()
    out = []
    for test, label in (("daily_10y", "10-year test on daily bars"), ("hourly_2y", "2-year test on hourly bars")):
        g = res[res.test == test]
        if g.empty:
            continue
        rows = [f"<tr><td class='l'>{r.instrument}</td><td class='l'>{r.strategy}</td><td>{r.target_atr:g}×</td>"
                f"<td>{r.stop_atr:g}×</td><td>{int(r.trades)}</td><td>{_f(r.trades_per_month, 1)}</td>"
                f"<td>{_f(r['success%'], 1)}</td><td class='{_c(r.exp_R)}'>{_f(r.exp_R, 3, True)}</td>"
                f"<td class='{_c(r.exp_R_h1)}'>{_f(r.exp_R_h1, 3, True)}</td><td class='{_c(r.exp_R_h2)}'>{_f(r.exp_R_h2, 3, True)}</td>"
                f"<td>{_f(r.t_stat, 2)}</td><td>{_f(r.max_dd_R, 1)}</td><td>{_f(r.avg_hold_d, 1)}</td>"
                f"<td class='l'><span class='chip {'rec' if r.verdict == 'robust edge' else ''}'>{r.verdict}</span></td></tr>"
                for _, r in g.iterrows()]
        lists = []
        if not trades.empty:
            for _, r in g.head(12).iterrows():
                t = trades[(trades.test == test) & (trades.instrument == r.instrument) & (trades.strategy == r.strategy)
                           & (trades.target_atr == r.target_atr) & (trades.stop_atr == r.stop_atr)].sort_values("entry_ts", ascending=False)
                if t.empty:
                    continue
                body = "".join(
                    f"<tr><td>{_ist(x.entry_ts, True)}</td><td class='l {'pos' if x.side == 'long' else 'neg'}'>{x.side}</td>"
                    f"<td>{_f(x.entry)}</td><td>{_f(x.stop)}</td><td>{_f(x.target)}</td><td>{_ist(x.exit_ts, True)}</td>"
                    f"<td>{_f(x.exit)}</td><td class='l'>{str(x.outcome).replace('_', ' ')}</td>"
                    f"<td class='{_c(x.R)}'>{_f(x.R, 2, True)}</td></tr>" for _, x in t.iterrows())
                lists.append(f"<details><summary><b>{r.instrument} {r.strategy} {r.target_atr:g}×/{r.stop_atr:g}× ATR</b> — "
                             f"{len(t)} trades, {int((t.R > 0).sum())} profitable, total {_f(t.R.sum(), 1, True)}R ▸</summary>"
                             "<div class='card' style='margin:6px 0 12px'><table><tr><th>Entry (IST)</th><th class='l'>Side</th>"
                             "<th>Entry</th><th>Stop</th><th>Target</th><th>Exit (IST)</th><th>Exit</th><th class='l'>Result</th>"
                             "<th>R</th></tr>" + body + "</table></div></details>")
        out.append(f"<h2>Backtest — {label} ({html.escape(str(g.span.iloc[0]))})</h2><div class='card'>" + _table(
            ["Instrument", "Strategy", "Target", "Stop", "Trades", "Per month", "Success %", "Exp R", "1st half", "2nd half",
             "t", "Max DD (R)", "Avg hold (d)", "Verdict"], rows, 2) + "</div>"
            + (("<h3 style='font-size:14px;margin:14px 0 6px'>Trades — top 12 combos, click to open</h3><div style='font-size:13px'>"
                + "".join(lists) + "</div>") if lists else ""))
    return "".join(out)


def render(cfg: dict | None = None, log=print) -> str:
    cfg = cfg or config.load()
    st = {}
    if os.path.isfile(engine.STATUS):
        with open(engine.STATUS) as f:
            st = json.load(f)
    fx = st.get("usdinr")
    kp = "".join(
        f"<div class='kpi'><b>{_f(v[0], 2)}</b><span>{k} {cfg['instruments'][k]['unit']} {_inr(v[0], fx)} · {_ist(v[1])} IST</span></div>"
        for k, v in st.get("prices", {}).items())
    if fx:
        kp += f"<div class='kpi'><b>{fx:.2f}</b><span>USD/INR</span></div>"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Commodity Swing Trading</title><style>{CSS}</style></head><body><main>
<nav><a href="../index.html">Leaderboard</a><a href="../daily.html">Daily results</a><a href="../crypto/index.html">Crypto</a><a href="index.html">Commodities</a></nav>
<h1>WTI crude &amp; natural gas — swing paper trades</h1>
<p class="sub">Updated {pd.Timestamp.now(tz=IST):%d %b %Y %H:%M} IST · Yahoo Finance front-month futures (CL=F, NG=F), ~15 min delayed ·
costs {cfg['cost_per_side'] * 200:.2f}% round trip · trade through MCX CRUDEOIL / NATURALGAS futures · signals only, no orders sent</p>
<div class="kpis">{kp}</div>
{_live_section(cfg)}
<p class="note"><b>How to read:</b> R = profit or loss in units of the stop (target 3×ATR with a 1×ATR stop = +3R if hit, −1R if stopped).
<b>Robust edge</b> = profitable after costs in both halves of the test, ≥ {cfg['min_trades']} trades and t-stat ≥ {cfg['min_t_stat']:g}.
The bar is higher than for crypto (t ≥ 2.5 here vs 2 there) because 56 combinations are tested: on pure random price data a t ≥ 2 rule still flagged up to 3 of 56 as robust by luck. Continuous futures data has small jumps at monthly contract rolls; treat single trades around a roll with caution.</p>
{_backtest_section()}
</main></body></html>"""
    path = os.path.join(config.REPORT_DIR, "commodity.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"commodity page: {path}")
    return path
