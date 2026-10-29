"""
swing_live.py — forward paper trading of the swing candidates from the 3-year test.

Forward-only: the clock starts at the first live run (state/swing_live.json)
and only signals known AFTER that moment count. Each run re-simulates from the
start date on the cached candles (deterministic), so a trade's state —
waiting, open, closed — is simply recomputed. Nothing is sent to an exchange.
"""

from __future__ import annotations

import html
import json
import os

import numpy as np
import pandas as pd

from . import config, swing, levels

# (coin, strategy, target, stop) — chosen from the 3-year backtest (21 Oct 2026)
CANDIDATES = [
    ("ETH", "donchian_20", 5000, 1500),
    ("ETH", "donchian_20", 3000, 1500),
    ("ETH", "donchian_55", 5000, 1500),
    ("ETH", "donchian_55", 3000, 2500),
    ("BTC", "donchian_20", 5000, 1500),
    ("BTC", "donchian_55", 5000, 1500),
    ("BTC", "week_break", 5000, 1500),
    ("BTC", "bos_pullback", 5000, 2500),
]
BACKTEST_R = {  # 3-year expectancy per trade, for comparison on the page
    ("ETH", "donchian_20", 5000, 1500): 0.456, ("ETH", "donchian_20", 3000, 1500): 0.256,
    ("ETH", "donchian_55", 5000, 1500): 0.456, ("ETH", "donchian_55", 3000, 2500): 0.290,
    ("BTC", "donchian_20", 5000, 1500): 0.109, ("BTC", "donchian_55", 5000, 1500): 0.061,
    ("BTC", "week_break", 5000, 1500): 0.224, ("BTC", "bos_pullback", 5000, 2500): 0.280,
}
STATE = os.path.join(config.STATE_DIR, "swing_live.json")
TRADES = os.path.join(config.REPORT_DIR, "swing_live_trades.csv")
STATUS = os.path.join(config.REPORT_DIR, "swing_live_status.json")


def _start() -> pd.Timestamp:
    if os.path.isfile(STATE):
        with open(STATE) as f:
            return pd.Timestamp(json.load(f)["start"])
    t = pd.Timestamp.now(tz="UTC").floor("h")
    with open(STATE, "w") as f:
        json.dump({"start": t.isoformat()}, f)
    return t


def _signals(name, df, d):
    if name == "donchian_20":
        return swing.sig_donchian(d, 20)
    if name == "donchian_55":
        return swing.sig_donchian(d, 55)
    if name == "week_break":
        return swing.sig_week_break(d)
    if name == "bos_pullback":
        return swing.sig_bos(df, pullback=True)
    raise ValueError(name)


def _watch(name, df, d) -> str:
    """The levels that would trigger the next signal."""
    if name.startswith("donchian"):
        n = int(name.split("_")[1])
        done = d.iloc[:-1]                                  # last daily bar is still forming
        hi, lo = done["High"].tail(n).max(), done["Low"].tail(n).min()
        return f"daily close > {hi:,.2f} → long · < {lo:,.2f} → short"
    if name == "week_break":
        w = d.resample("W-SUN").agg({"High": "max", "Low": "min"})
        if len(w) < 2:
            return "–"
        return f"daily close > {w['High'].iloc[-2]:,.2f} → long · < {w['Low'].iloc[-2]:,.2f} → short"
    if name == "bos_pullback":
        piv = levels.pivots(df.tail(12 * 24 * 60), wing=3, tf="6h")
        hs = [p for p in piv if p["kind"] == "H"]
        ls = [p for p in piv if p["kind"] == "L"]
        if not hs or not ls:
            return "–"
        return (f"6h close > {hs[-1]['p']:,.2f} → buy pullback to 50% · "
                f"< {ls[-1]['p']:,.2f} → sell pullback to 50%")
    return "–"


def update(frames: dict, cfg: dict | None = None, log=print) -> None:
    cfg = cfg or config.load()
    cost = cfg["fee_per_side"] + cfg["slippage_per_side"]
    start = _start()
    btc_h1 = swing._h1(frames["BTC"])["Close"]
    rows, status = [], []
    for coin, name, tgt, stp in CANDIDATES:
        df = frames.get(coin)
        if df is None or df.empty:
            continue
        h1, d = swing._h1(df), swing._daily(df)
        ref = btc_h1.reindex(h1.index, method="ffill").bfill()
        sigs = [s for s in _signals(name, df, d) if s[0] >= start]
        tr = swing.run_signals(sigs, h1, tgt / ref, stp / ref, cost, include_open=True)
        for t in tr:
            t.update({"coin": coin, "strategy": name, "btc_target": tgt, "btc_stop": stp})
        rows += tr
        open_t = next((t for t in tr if t["status"] == "open"), None)
        closed = [t for t in tr if t["status"] == "closed"]
        status.append({
            "coin": coin, "strategy": name, "btc_target": tgt, "btc_stop": stp,
            "backtest_R": BACKTEST_R.get((coin, name, tgt, stp)),
            "position": open_t, "watch": _watch(name, df, d),
            "closed": len(closed), "wins": sum(1 for t in closed if t["net_pts"] > 0),
            "total_R": round(sum(t["R"] for t in closed), 2),
            "last_price": float(df["Close"].iloc[-1]),
        })
    pd.DataFrame(rows).to_csv(TRADES, index=False)
    with open(os.path.join(config.STATE_DIR, "last_prices.json"), "w") as f:
        json.dump({c: [float(df["Close"].iloc[-1]), str(df.index[-1])] for c, df in frames.items() if not df.empty}, f)
    with open(STATUS, "w") as f:
        json.dump({"start": start.isoformat(), "updated": pd.Timestamp.now(tz="UTC").isoformat(),
                   "status": status}, f, default=str)
    try:
        from . import notify
        notify.check(status, rows, log=log)
    except Exception as e:                      # alerts must never break the tracker
        log(f"alerts failed: {type(e).__name__}: {e}")
    n_open = sum(1 for s in status if s["position"])
    log(f"swing live: {len(CANDIDATES)} strategies, {n_open} open, "
        f"{sum(s['closed'] for s in status)} closed since {start:%Y-%m-%d %H:%M} UTC")


def _f(v, nd=2, signed=False):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if np.isnan(v):
        return "–"
    return f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"


def _c(v):
    try:
        return "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
    except (TypeError, ValueError):
        return ""


def section() -> str:
    if not os.path.isfile(STATUS):
        return ""
    with open(STATUS) as f:
        st = json.load(f)
    start = pd.Timestamp(st["start"]).tz_convert("Asia/Kolkata")
    rows = []
    for s in st["status"]:
        p = s["position"]
        if p:
            pos = (f"<b class='{'pos' if p['side'] == 'long' else 'neg'}'>{p['side'].upper()}</b> @ {_f(p['entry'])}"
                   f"<br><span class='note'>stop {_f(p['stop'])} · target {_f(p['target'])} · time stop "
                   f"{pd.Timestamp(p['time_stop']).tz_convert('Asia/Kolkata'):%d %b %H:%M} IST</span>")
            pnl = f"<span class='{_c(p['unreal_R'])}'>{_f(p['unreal_pts'], 1, True)} pts ({_f(p['unreal_R'], 2, True)}R)</span>"
        else:
            pos, pnl = "flat — waiting for signal", "–"
        winp = f"{s['wins'] / s['closed'] * 100:.0f}%" if s["closed"] else "–"
        rows.append(
            f"<tr><td class='l'><b>{s['coin']}</b></td><td class='l'>{html.escape(s['strategy'])}</td>"
            f"<td>{s['btc_target']}/{s['btc_stop']}</td><td class='l'>{pos}</td><td>{pnl}</td>"
            f"<td class='l note'>{html.escape(s['watch'])}</td><td>{s['closed']}</td><td>{winp}</td>"
            f"<td class='{_c(s['total_R'])}'>{_f(s['total_R'], 2, True)}</td><td>{_f(s['backtest_R'], 2, True)}</td></tr>")
    head = "".join(f"<th class='{'l' if i in (0, 1, 3, 5) else ''}'>{h}</th>" for i, h in enumerate(
        ["Coin", "Strategy", "Target/Stop", "Position", "Open P&L", "Next signal if…", "Closed", "Win %", "Total R", "Backtest R/trade"]))
    closed_html = ""
    if os.path.isfile(TRADES) and os.path.getsize(TRADES) > 5:
        t = pd.read_csv(TRADES)
        # exit_ts only exists once a trade has closed; with only open trades the
        # column is missing and sorting on it used to crash the whole section.
        if "status" in t.columns and "exit_ts" in t.columns:
            t = t[t.status == "closed"].sort_values("exit_ts", ascending=False)
        else:
            t = t.iloc[0:0]
        if not t.empty:
            tr = "".join(
                f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(r.strategy)}</td><td>{int(r.btc_target)}/{int(r.btc_stop)}</td><td class='l'>{r.side}</td>"
                f"<td>{pd.Timestamp(r.entry_ts).tz_convert('Asia/Kolkata'):%d %b %H:%M}</td><td>{_f(r.entry)}</td>"
                f"<td>{pd.Timestamp(r.exit_ts).tz_convert('Asia/Kolkata'):%d %b %H:%M}</td><td>{_f(r.exit)}</td>"
                f"<td class='l'>{r.outcome.replace('_', ' ')}</td><td class='{_c(r.net_pts)}'>{_f(r.net_pts, 1, True)}</td>"
                f"<td class='{_c(r.R)}'>{_f(r.R, 2, True)}</td></tr>" for _, r in t.head(50).iterrows())
            closed_html = ("<h3 style='font-size:14px;margin:16px 0 6px'>Closed swing trades</h3><div class='card'><table><tr>"
                           "<th class='l'>Coin</th><th class='l'>Strategy</th><th>Target/Stop</th><th class='l'>Side</th><th>Entry (IST)</th><th>Entry</th>"
                           "<th>Exit (IST)</th><th>Exit</th><th class='l'>Result</th><th>Net pts</th><th>R</th></tr>" + tr + "</table></div>")
    return (f"<h2>Swing paper trading — live since {start:%d %b %Y %H:%M} IST</h2>"
            "<p class='note'>Strategies that held up in the 3-year test (multi-day holds, max 10 days, one position at a time each). "
            "Signals use CLOSED daily / 6-hour bars; entry at the next hour's open. Expect a low win rate (30–45 %) with winners 2–3× "
            "losers, and only 1–2 trades a month per strategy — judge after months, not days. Paper only; no orders are sent.</p>"
            f"<div class='card'><table><tr>{head}</tr>{''.join(rows)}</table></div>{closed_html}")
