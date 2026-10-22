"""page.py — the Nifty 200 page (nifty200/index.html on the dashboard): intraday + swing."""

from __future__ import annotations

import html
import json
import os

import pandas as pd

from . import config, intraday, live, swing

IST = "Asia/Kolkata"
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5b6472;--line:#e3e6eb;--pos:#127a4a;--neg:#b42318;--acc:#2754c5;--chip:#eef2fb;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--mute:#98a2b3;--line:#262c35;--pos:#47c38a;--neg:#f27c70;--acc:#7da2ff;--chip:#1e2633;--warn:#e0b04a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 6px;padding-top:10px;border-top:2px solid var(--line)}
h3{font-size:14px;margin:20px 0 6px}.sub,.note{color:var(--mute)}.note{font-size:12.5px;max-width:940px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600}td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:12px}
.rec{background:var(--pos);color:#fff}nav a{color:var(--acc);text-decoration:none;margin-right:14px;font-weight:600}
.jump a{color:var(--acc);margin-right:12px}details summary{cursor:pointer;color:var(--acc);margin:8px 0}
"""
NAV = ('<nav><a href="../index.html">Leaderboard</a><a href="../daily.html">Daily results</a>'
       '<a href="index.html">Nifty 200</a><a href="../crypto/index.html">Crypto</a></nav>')


def _f(v, nd=2, signed=False):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if pd.isna(v):
        return "–"
    return f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"


def _c(v):
    try:
        return "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
    except (TypeError, ValueError):
        return ""


def _t(ts, fmt="%d %b %H:%M"):
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or str(ts) in ("", "nan", "NaT", "None"):
        return "–"
    t = pd.Timestamp(ts)
    return (t.tz_convert(IST) if t.tzinfo else t).strftime(fmt)


def _table(head, rows, left=()):
    if not rows:
        return "<p class='note' style='padding:10px'>Nothing yet.</p>"
    h = "".join(f"<th class='{'l' if i in left else ''}'>{x}</th>" for i, x in enumerate(head))
    return f"<div class='card'><table><tr>{h}</tr>{''.join(rows)}</table></div>"


def _read(p):
    return pd.read_csv(p) if os.path.isfile(p) and os.path.getsize(p) > 5 else pd.DataFrame()


def _sym(s):
    return html.escape(str(s).replace(".NS", ""))


def _status(t):
    if t.status == "pending":
        return "<span class='chip'>signal — enter next bar</span>"
    if t.status == "open":
        return "<span class='chip rec'>open</span>"
    return html.escape(str(t.outcome))


def _verdict_chip(v):
    return f"<span class='chip {'rec' if v == 'robust edge' else ''}'>{html.escape(str(v))}</span>"


# ── intraday section ─────────────────────────────────────────────────────────
def _intraday(sel, now) -> str:
    if not sel.get("intraday"):
        return ("<h2 id='intraday'>Intraday setups — switched off</h2>"
                "<p class='note'><b>No intraday setup made money after costs</b> (opening-range breakouts, stocks in play, "
                "yesterday's high/low breaks, gap-and-go, gap fade — long and short, 1R / 2R / hold to close). "
                "Live tracking and alerts are off; the backtest is kept below for reference.</p>" + _backtest_intraday())
    tr = _read(live.INTRA_TR)
    picked = "".join(
        f"<li><b>{html.escape(intraday.LABEL.get(c['strategy'], c['strategy']))}</b> · {c['side']} · target {c['target']}"
        f" — backtest {_f(c['backtest_R_1h'], 2, True)}R/trade (1-hour bars), {_f(c.get('backtest_R_15m'), 2, True)}R (60 days, 15-min)"
        f"{' · <span class=warn>experimental: no setup passed the checks</span>' if c['experimental'] else ''}</li>"
        for c in sel.get("intraday", []))
    out = ["<h2 id='intraday'>Intraday setups — 15-minute bars</h2>",
           "<p class='note'>Signals use CLOSED 15-minute bars; paper entry at the next bar's open; stop and target as listed; "
           "everything is squared off at 15:15. At most 5 signals per setup per day (earliest first). "
           "Prices from Yahoo (may lag a few minutes) — treat an alert as a prompt to check the chart, not an order.</p>",
           f"<h3>Setups being tracked</h3><ul class='note'>{picked or '<li>none yet — run the backtest</li>'}</ul>"]
    if not tr.empty:
        tr["signal_ts"] = pd.to_datetime(tr["signal_ts"], utc=True)
        today = tr[tr.signal_ts.dt.tz_convert(IST).dt.date == now.date()].sort_values("signal_ts", ascending=False)
        rows = [f"<tr><td>{_t(t.signal_ts, '%H:%M')}</td><td class='l'><b>{_sym(t.sym)}</b></td>"
                f"<td class='l'>{html.escape(t.strategy)} · {t.target}</td><td class='l {'pos' if t.side == 'long' else 'neg'}'>{t.side}</td>"
                f"<td>{_f(t.entry)}</td><td>{_f(t.stop)}</td><td>{_f(t.target_px) if t.target != 'eod' else '15:15'}</td>"
                f"<td>{_f(t['last'])}</td><td class='l'>{_status(t)}</td><td class='{_c(t.R)}'>{_f(t.R, 2, True)}</td><td>{int(t.qty)}</td></tr>"
                for _, t in today.iterrows()]
        out.append(f"<h3>Today ({now:%d %b})</h3>")
        out.append(_table(["Signal", "Stock", "Setup", "Side", "Entry", "Stop", "Target", "Last", "Status", "R", "Qty*"], rows, (1, 2, 3, 8)))
        closed = tr[tr.status == "closed"]
        if not closed.empty:
            g = closed.groupby(["strategy", "target"]).agg(trades=("R", "size"), win=("R", lambda r: (r > 0).mean() * 100),
                                                           avg=("R", "mean"), total=("R", "sum")).reset_index()
            rows = [f"<tr><td class='l'>{html.escape(r.strategy)} · {r.target}</td><td>{r.trades}</td><td>{r.win:.0f}%</td>"
                    f"<td class='{_c(r.avg)}'>{_f(r.avg, 2, True)}</td><td class='{_c(r.total)}'>{_f(r.total, 1, True)}</td></tr>"
                    for _, r in g.iterrows()]
            out.append("<h3>Paper record since live start</h3>" + _table(["Setup", "Trades", "Win %", "Avg R", "Total R"], rows, (0,)))
            last = closed.sort_values("signal_ts", ascending=False).head(60)
            rows = [f"<tr><td>{_t(t.signal_ts)}</td><td class='l'>{_sym(t.sym)}</td><td class='l'>{html.escape(t.strategy)} · {t.target}</td>"
                    f"<td class='l'>{t.side}</td><td>{_f(t.entry)}</td><td>{_f(t.exit)}</td><td>{_t(t.exit_ts, '%H:%M')}</td>"
                    f"<td class='l'>{t.outcome}</td><td class='{_c(t.R)}'>{_f(t.R, 2, True)}</td></tr>" for _, t in last.iterrows()]
            out.append("<details><summary>Closed intraday trades (latest 60) ▸</summary>"
                       + _table(["Signal", "Stock", "Setup", "Side", "Entry", "Exit", "Exit time", "Result", "R"], rows, (1, 2, 3, 7)) + "</details>")
    out.append(_backtest_intraday())
    return "\n".join(out)


def _backtest_intraday() -> str:
    res = _read(os.path.join(config.REPORT_DIR, "intraday_backtest.csv"))
    if res.empty:
        return ""
    res = res[res.side != "both"]
    parts = []
    for tf, label in (("1h", "1-hour bars (opening range = first hour)"), ("15m", "last 60 days, 15-minute bars")):
        r = res[res.tf == tf].sort_values("exp_R", ascending=False).head(15)
        if r.empty:
            continue
        rows = [f"<tr><td class='l'>{html.escape(intraday.LABEL.get(x.strategy, x.strategy))}</td><td>{x.target}</td><td class='l'>{x.side}</td>"
                f"<td>{int(x.trades)}</td><td>{_f(x.per_day, 1)}</td><td>{_f(x['success%'], 1)}</td><td class='{_c(x.exp_R)}'>{_f(x.exp_R, 3, True)}</td>"
                f"<td class='{_c(x.exp_R_h1)}'>{_f(x.exp_R_h1, 3, True)}</td><td class='{_c(x.exp_R_h2)}'>{_f(x.exp_R_h2, 3, True)}</td>"
                f"<td>{_f(x.t_stat, 2)}</td><td>{_f(x.random_R, 3, True)}</td><td class='l'>{_verdict_chip(x.verdict)}</td></tr>"
                for _, x in r.iterrows()]
        parts.append(f"<h3>Backtest — {label} · {html.escape(str(r['span'].iloc[0]))}</h3>"
                     + _table(["Setup", "Target", "Side", "Trades", "Per day", "Success %", "Exp R", "1st half", "2nd half", "t", "Random R", "Verdict"], rows, (0, 2, 11)))
    return "\n".join(parts)


# ── swing section ────────────────────────────────────────────────────────────
def _swing(sel) -> str:
    tr = _read(live.SWING_TR)
    picked = "".join(
        f"<li><b>{html.escape(swing.LABEL.get(c['strategy'], c['strategy']))}</b> · exit {html.escape(c['exit'])}"
        f"{' · only when Nifty is above its 200-day average' if c['market_filter'] else ''}"
        f" — backtest {_f(c['backtest_R'], 2, True)}R/trade ({html.escape(c['verdict'])})"
        f"{' · <span class=warn>experimental</span>' if c['experimental'] else ''}</li>" for c in sel.get("swing", []))
    out = ["<h2 id='swing'>Swing setups — daily bars, long only</h2>",
           "<p class='note'>Signal on the daily close (checked after 16:00 IST); paper entry at the next day's open; stop and "
           "target in multiples of the 14-day ATR; exit after 20 sessions at the latest. Delivery costs (STT etc.) included.</p>",
           f"<h3>Setups being tracked</h3><ul class='note'>{picked or '<li>none yet — run the backtest</li>'}</ul>"]
    if not tr.empty:
        act = tr[tr.status.isin(["pending", "open"])].sort_values("signal_ts", ascending=False)
        rows = [f"<tr><td>{_t(t.signal_ts, '%d %b')}</td><td class='l'><b>{_sym(t.sym)}</b></td><td class='l'>{html.escape(t.strategy)}</td>"
                f"<td>{_t(t.entry_ts, '%d %b')}</td><td>{_f(t.entry)}</td><td>{_f(t.stop)}</td><td>{_f(t.target_px)}</td><td>{_f(t['last'])}</td>"
                f"<td class='l'>{'<span class=chip>buy at next open</span>' if t.status == 'pending' else '<span class=\"chip rec\">open</span>'}</td>"
                f"<td class='{_c(t.R)}'>{_f(t.R, 2, True)}</td><td>{int(t.qty)}</td></tr>" for _, t in act.iterrows()]
        out.append("<h3>New signals and open positions</h3>"
                   + _table(["Signal", "Stock", "Setup", "Entry date", "Entry", "Stop", "Target", "Last close", "Status", "R", "Qty*"], rows, (1, 2, 8)))
        closed = tr[tr.status == "closed"].sort_values("exit_ts", ascending=False)
        if not closed.empty:
            g = closed.groupby("strategy").agg(trades=("R", "size"), win=("R", lambda r: (r > 0).mean() * 100),
                                               avg=("R", "mean"), total=("R", "sum")).reset_index()
            rows = [f"<tr><td class='l'>{html.escape(r.strategy)}</td><td>{r.trades}</td><td>{r.win:.0f}%</td>"
                    f"<td class='{_c(r.avg)}'>{_f(r.avg, 2, True)}</td><td class='{_c(r.total)}'>{_f(r.total, 1, True)}</td></tr>" for _, r in g.iterrows()]
            out.append("<h3>Paper record since live start</h3>" + _table(["Setup", "Trades", "Win %", "Avg R", "Total R"], rows, (0,)))
            rows = [f"<tr><td class='l'>{_sym(t.sym)}</td><td class='l'>{html.escape(t.strategy)}</td><td>{_t(t.entry_ts, '%d %b')}</td><td>{_f(t.entry)}</td>"
                    f"<td>{_t(t.exit_ts, '%d %b')}</td><td>{_f(t.exit)}</td><td class='l'>{t.outcome}</td><td class='{_c(t.R)}'>{_f(t.R, 2, True)}</td></tr>"
                    for _, t in closed.head(60).iterrows()]
            out.append("<details><summary>Closed swing trades ▸</summary>"
                       + _table(["Stock", "Setup", "Entry", "Price", "Exit", "Price", "Result", "R"], rows, (0, 1, 6)) + "</details>")
    w = _read(live.WATCH)
    if not w.empty:
        w = w.sort_values("gap%").head(40)
        rows = [f"<tr><td class='l'>{_sym(x.sym)}</td><td class='l'>{html.escape(swing.LABEL.get(x.strategy, x.strategy))}</td>"
                f"<td>{_f(x.close)}</td><td>{_f(x.trigger)}</td><td>{_f(x['gap%'], 2)}%</td></tr>" for _, x in w.iterrows()]
        out.append("<h3>Watchlist — within 2 % of a trigger (a close above it fires the setup)</h3>"
                   + _table(["Stock", "Setup", "Close", "Trigger", "Distance"], rows, (0, 1)))
    out.append(_backtest_swing())
    return "\n".join(out)


def _backtest_swing() -> str:
    res = _read(os.path.join(config.REPORT_DIR, "swing_backtest.csv"))
    if res.empty:
        return ""
    r = res.sort_values("exp_R", ascending=False).head(20)
    rows = [f"<tr><td class='l'>{html.escape(swing.LABEL.get(x.strategy, x.strategy))}</td><td>{'yes' if x.market_filter else '–'}</td>"
            f"<td>{html.escape(x.exit_rule)}</td><td>{int(x.trades)}</td><td>{_f(x.per_month, 1)}</td><td>{_f(x['success%'], 1)}</td>"
            f"<td class='{_c(x.exp_R)}'>{_f(x.exp_R, 3, True)}</td><td class='{_c(x.exp_R_h1)}'>{_f(x.exp_R_h1, 3, True)}</td>"
            f"<td class='{_c(x.exp_R_h2)}'>{_f(x.exp_R_h2, 3, True)}</td><td>{html.escape(str(x.years_positive))}</td><td>{_f(x.t_stat, 2)}</td>"
            f"<td>{_f(x.random_R, 3, True)}</td><td>{_f(x.avg_hold_d, 1)}</td><td class='l'>{_verdict_chip(x.verdict)}</td></tr>" for _, x in r.iterrows()]
    return (f"<h3>Backtest — 10 years, daily bars · {html.escape(str(r['span'].iloc[0]))}</h3>"
            + _table(["Setup", "Nifty filter", "Exit (stop/target)", "Trades", "Per month", "Success %", "Exp R", "1st half", "2nd half",
                      "Years +", "t", "Random R", "Avg hold (d)", "Verdict"], rows, (0, 13)))


def render(cfg: dict | None = None, log=print) -> str:
    sel = live.load_selection(cfg)
    now = live.now_ist()
    st = {}
    if os.path.isfile(live.STATUS):
        with open(live.STATUS) as f:
            st = json.load(f)
    since = f" · paper trading since {_t(st['start'], '%d %b %Y %H:%M')} IST" if st.get("start") else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Nifty 200 Screener</title><style>{CSS}</style></head><body><main>
{NAV}
<h1>Nifty 200 screener — intraday and swing</h1>
<p class="sub">Updated {now:%d %b %Y %H:%M} IST{since} · signals and paper trades only, no orders are sent</p>
<p class="jump"><a href="#intraday">Intraday ↓</a><a href="#swing">Swing ↓</a></p>
<p class="note"><b>How to read it.</b> <b>R</b> = profit per trade in units of the amount risked (entry − stop). <b>Exp R</b> = average R per trade
after costs. <b>Random R</b> = what the same stops and targets earn with random entry times — a setup only has an edge if it beats this.
<b>Robust edge</b> = profitable in both halves of the test, at least 30 trades, t ≥ 2.5 (computed on per-day / per-week averages, so
fifty stocks moving together on one day count once), and at least 0.10R better than random entries.
*Qty = shares for a ₹{(cfg or config.load())['risk_per_trade_inr']:,} risk between entry and stop.</p>
{_intraday(sel, now)}
{_swing(sel)}
<p class="note" style="margin-top:28px">Caveat: the stock list is today's Nifty 200, so the 10-year swing test includes survivorship bias
(stocks that fell out of the index are missing). The random-entry column is measured on the same stocks, so "beats random" is the fair comparison.</p>
</main></body></html>"""
    path = os.path.join(config.REPORT_DIR, "nifty200.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"nifty page: {path}")
    return path
