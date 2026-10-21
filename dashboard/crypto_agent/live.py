"""
live.py — the Crypto page (crypto/index.html on the dashboard).

Shows the live swing paper trading (swing_live.py) and the multi-day swing
backtest. The intraday 1000-point strategies and the intraday level backtest
were removed on 21 Oct 2026 — no edge after costs over 3 years.
"""

from __future__ import annotations

import html
import json
import os

import pandas as pd

from . import config

IST = "Asia/Kolkata"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5b6472;--line:#e3e6eb;--pos:#127a4a;--neg:#b42318;--acc:#2754c5;--chip:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--mute:#98a2b3;--line:#262c35;--pos:#47c38a;--neg:#f27c70;--acc:#7da2ff;--chip:#1e2633}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub,.note{color:var(--mute)}.note{font-size:12.5px;max-width:920px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600}td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
.pos{color:var(--pos)}.neg{color:var(--neg)}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:12px}
.rec{background:var(--pos);color:#fff}nav a{color:var(--acc);text-decoration:none;margin-right:14px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--mute);font-size:12px}
"""


def _f(v, nd=2, signed=False):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    return f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"


def _c(v):
    try:
        return "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
    except (TypeError, ValueError):
        return ""


def _ist(ts, year=False):
    if not isinstance(ts, (str, pd.Timestamp)) or not str(ts) or str(ts) == "nan":
        return "–"
    try:
        return pd.Timestamp(ts).tz_convert(IST).strftime("%d %b %Y %H:%M" if year else "%d %b %H:%M")
    except Exception:
        return str(ts)

def _table(head, rows, left=1):
    h = "".join(f"<th class='{'l' if i < left else ''}'>{x}</th>" for i, x in enumerate(head))
    return f"<table><tr>{h}</tr>{''.join(rows)}</table>" if rows else "<p class='note' style='padding:10px'>Nothing yet.</p>"


def _swing_section() -> str:
    p = os.path.join(config.REPORT_DIR, "swing_backtest.csv")
    if not os.path.isfile(p):
        return ""
    d = pd.read_csv(p).head(25)
    tp = os.path.join(config.REPORT_DIR, "swing_trades.csv.gz")
    trades = pd.read_csv(tp) if os.path.isfile(tp) else pd.DataFrame()
    rows = [f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(str(r.strategy))}</td><td>{int(r.btc_target)}</td>"
            f"<td>{int(r.btc_stop)}</td><td>{int(r.trades)}</td><td>{_f(r.trades_per_month, 1)}</td><td>{_f(r['success%'], 1)}</td>"
            f"<td class='{_c(r.exp_R)}'>{_f(r.exp_R, 3, True)}</td><td class='{_c(r.exp_R_h1)}'>{_f(r.exp_R_h1, 3, True)}</td>"
            f"<td class='{_c(r.exp_R_h2)}'>{_f(r.exp_R_h2, 3, True)}</td><td>{_f(r.t_stat, 2)}</td><td>{_f(r.avg_hold_d, 1)}</td>"
            f"<td class='l'><span class='chip {'rec' if r.verdict == 'robust edge' else ''}'>{r.verdict}</span></td></tr>"
            for _, r in d.iterrows()]
    lists = []
    if not trades.empty:
        for _, r in d.iterrows():
            t = trades[(trades.coin == r.coin) & (trades.strategy == r.strategy)
                       & (trades.btc_target == r.btc_target) & (trades.btc_stop == r.btc_stop)]
            if t.empty:
                continue
            t = t.sort_values("entry_ts", ascending=False)
            body = []
            for _, x in t.iterrows():
                sgn = 1 if x.side == "long" else -1
                stop = x.entry - sgn * x.stop_pts
                tgt = x.entry + sgn * x.target_pts
                body.append(
                    f"<tr><td>{_ist(x.entry_ts, True)}</td><td class='l {'pos' if x.side == 'long' else 'neg'}'>{x.side}</td>"
                    f"<td>{_f(x.entry)}</td><td>{_f(stop)}</td><td>{_f(tgt)}</td><td>{_ist(x.exit_ts, True)}</td><td>{_f(x.exit)}</td>"
                    f"<td class='l'>{str(x.outcome).replace('_', ' ')}</td>"
                    f"<td class='{_c(x.net_pts)}'>{_f(x.net_pts, 1, True)}</td><td class='{_c(x.R)}'>{_f(x.R, 2, True)}</td></tr>")
            wins = int((t.net_pts > 0).sum())
            lists.append(
                f"<details><summary><b>{r.coin} {html.escape(str(r.strategy))} {int(r.btc_target)}/{int(r.btc_stop)}</b> — "
                f"{len(t)} trades, {wins} profitable, total {_f(t.R.sum(), 1, True)}R ▸</summary>"
                "<div class='card' style='margin:6px 0 12px'><table><tr><th>Entry (IST)</th><th class='l'>Side</th><th>Entry</th>"
                "<th>Stop</th><th>Target</th><th>Exit (IST)</th><th>Exit</th><th class='l'>Result</th><th>Net pts</th><th>R</th></tr>"
                + "".join(body) + "</table></div></details>")
    trade_html = ("<h3 style='font-size:14px;margin:18px 0 6px'>Backtest trades — click a strategy to see every trade (daily signals enter at 05:30 IST, the open after the UTC daily close)</h3>"
                  "<div style='font-size:13px'>" + "".join(lists) + "</div>") if lists else ""
    return ("<h2>Multi-day swing backtest " + html.escape(str(d['span'].iloc[0])) + "</h2>"
            "<p class='note'>Targets 3000 / 5000 BTC points (ETH same %), stops 1500 / 2500, max 10-day hold, one position at a time "
            "per strategy, fees included.<br><b>Robust edge</b> = passes all three checks: (1) profitable after fees in BOTH halves "
            "of the 3 years, so it is not one lucky period; (2) at least 20 trades; (3) t-stat ≥ 2, i.e. roughly a 1-in-40 chance "
            "or less that a strategy with no real edge would show a result this good. <b>Positive, not significant</b> = profitable "
            "in both halves but luck cannot be ruled out yet. <b>Exp R</b> = average profit per trade in units of risk "
            "(+0.46R with a 1500-point stop ≈ +690 BTC points per trade).</p>"
            "<div class='card'>" + _table(["Coin", "Strategy", "Target", "Stop", "Trades", "Per month", "Success %", "Exp R",
                                           "1st half", "2nd half", "t", "Avg hold (days)", "Verdict"], rows, 2) + "</div>"
            + trade_html)

def _swing_live() -> str:
    try:
        from . import swing_live
        return swing_live.section()
    except Exception as e:
        return f"<p class='note'>Swing tracker unavailable: {html.escape(str(e))}</p>"


def render(cfg: dict | None = None, log=print) -> str:
    cfg = cfg or config.load()
    prices = {}
    p = os.path.join(config.STATE_DIR, "last_prices.json")
    if os.path.isfile(p):
        with open(p) as f:
            prices = json.load(f)
    kp = "".join(f"<div class='kpi'><b>{_f(v[0], 2)}</b><span>{c} · {_ist(v[1])} IST</span></div>" for c, v in prices.items())
    cost = (cfg["fee_per_side"] + cfg["slippage_per_side"]) * 200
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Crypto Swing Trading</title><style>{CSS}</style></head><body><main>
<nav><a href="../index.html">Leaderboard</a><a href="../daily.html">Daily results</a><a href="index.html">Crypto</a><a href="../commodity/index.html">Commodities</a></nav>
<h1>BTC / ETH — swing paper trades</h1>
<p class="sub">Updated {pd.Timestamp.now(tz=IST):%d %b %Y %H:%M} IST · Hyperliquid prices · fees+slippage {cost:.2f}% round trip ·
signals only, no orders sent</p>
<div class="kpis">{kp}</div>
{_swing_live()}
{_swing_section()}
</main></body></html>"""
    path = os.path.join(config.REPORT_DIR, "crypto.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"crypto page: {path}")
    return path
