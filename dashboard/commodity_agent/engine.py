"""
engine.py — swing backtest and forward paper trading for WTI crude and natural gas.

Same method as the crypto swing test (crypto_agent/swing.py), reusing its
signal generators and execution:
  donchian_20 / donchian_55   daily close beyond the prior 20 / 55-day high or low
  week_break                  daily close beyond the previous week's high or low
  bos_pullback                6-hour break of structure, limit entry at 50 % of the leg
                              (hourly test only — needs intraday bars)
Exits: target / stop in multiples of the 14-day ATR (known at the signal),
time exit after `max_hold_days`, one position at a time per strategy,
stop-first on ambiguous bars, MCX-like costs on both sides.

Two tests:
  daily_10y   executed on daily bars (entry next day's open) — long sample
  hourly_2y   executed on hourly bars (entry next hour's open) — realistic fills
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from crypto_agent import swing as cs
from crypto_agent.stats import _stats

from . import config, data

STRATS_DAILY = ("donchian_20", "donchian_55", "week_break")
STRATS_HOURLY = STRATS_DAILY + ("bos_pullback",)


def atr_frac(d: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR / close, shifted so the value at day t only uses data through day t-1."""
    h, l, c = d["High"], d["Low"], d["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return (tr.rolling(n).mean() / c).shift(1)


def _signals(name: str, d: pd.DataFrame, h1: pd.DataFrame | None):
    if name == "donchian_20":
        return cs.sig_donchian(d, 20)
    if name == "donchian_55":
        return cs.sig_donchian(d, 55)
    if name == "week_break":
        return cs.sig_week_break(d)
    if name == "bos_pullback" and h1 is not None:
        return cs.sig_bos(h1, pullback=True)
    return []


def _run(sigs, bars: pd.DataFrame, af: pd.Series, t_mult: float, s_mult: float, cost: float,
         bars_per_day: int, include_open: bool = False) -> list[dict]:
    f = af.reindex(bars.index, method="ffill").bfill()
    return cs.run_signals(sigs, bars, f * t_mult, f * s_mult, cost, include_open=include_open,
                          bars_per_day=bars_per_day)


# ── backtest ─────────────────────────────────────────────────────────────────

def backtest(cfg: dict | None = None, log=print) -> pd.DataFrame:
    cfg = cfg or config.load()
    cost = cfg["cost_per_side"]
    cs.MAX_HOLD_DAYS = cfg["max_hold_days"]
    rows, trades = [], []
    for key, ins in cfg["instruments"].items():
        d = data.bars(key, ins["yf"], "1d", cfg, full=True, log=log)
        h1 = data.bars(key, ins["yf"], "1h", cfg, full=True, log=log)
        if d.empty:
            continue
        af = atr_frac(d)
        tests = [("daily_10y", d, 1, STRATS_DAILY, d)]
        if not h1.empty:
            tests.append(("hourly_2y", h1, 23, STRATS_HOURLY, d[d.index >= h1.index[0] - pd.Timedelta(days=90)]))
        for test, bars, bpd, strats, dsig in tests:
            days = sorted(set(bars.index.floor("D")))
            mid = days[len(days) // 2]
            span = f"{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}"
            for name in strats:
                sigs = [s for s in _signals(name, dsig, h1 if test == "hourly_2y" else None) if s[0] >= bars.index[0]]
                for tm, sm in cfg["atr_combos"]:
                    tr = pd.DataFrame(_run(sigs, bars, af, tm, sm, cost, bpd))
                    if tr.empty:
                        continue
                    tr = tr.assign(instrument=key, test=test, strategy=name, target_atr=tm, stop_atr=sm)
                    trades.append(tr)
                    s, a, b = _stats(tr), _stats(tr[tr.day < mid]), _stats(tr[tr.day >= mid])
                    rows.append({"instrument": key, "test": test, "strategy": name, "target_atr": tm, "stop_atr": sm,
                                 "trades_per_month": round(s["trades"] / (len(days) / 30.4), 2), **s,
                                 "exp_R_h1": a.get("exp_R"), "exp_R_h2": b.get("exp_R"),
                                 "avg_hold_d": round(float(tr.hold_d.mean()), 1), "span": span})
            log(f"backtest: {key} {test} {span}")
    res = pd.DataFrame(rows)
    if res.empty:
        log("backtest: no data")
        return res
    ok = ((res.trades >= cfg["min_trades"]) & (res.exp_R_h1 > 0) & (res.exp_R_h2 > 0)
          & (res.t_stat >= cfg["min_t_stat"]))
    res["verdict"] = np.where(ok, "robust edge",
                     np.where(res.trades < cfg["min_trades"], "too few trades",
                     np.where((res.exp_R_h1 > 0) & (res.exp_R_h2 > 0), "positive, not significant",
                     np.where(res.exp_R > 0, "unstable (one half only)", "no edge"))))
    res["robust_R"] = res[["exp_R_h1", "exp_R_h2"]].min(axis=1)
    res = res.sort_values(["test", "exp_R"], ascending=[True, False]).reset_index(drop=True)
    res.to_csv(os.path.join(config.REPORT_DIR, "commodity_backtest.csv"), index=False)
    pd.concat(trades).to_csv(os.path.join(config.REPORT_DIR, "commodity_trades.csv.gz"), index=False, compression="gzip")
    _select(res)
    log(f"backtest: {len(res)} combos, {int(ok.sum())} robust, {int((res.exp_R > 0).sum())} positive")
    return res


def _select(res: pd.DataFrame) -> None:
    """For live tracking: per instrument × strategy, the ATR combo that did best
    in its WEAKER half of the 10-year daily test (the hourly test for bos_pullback)."""
    sel = []
    for (ins, strat), g in res.groupby(["instrument", "strategy"]):
        pref = g[g.test == "daily_10y"] if (g.test == "daily_10y").any() else g
        r = pref.sort_values("robust_R", ascending=False).iloc[0]
        sel.append({"instrument": ins, "strategy": strat, "target_atr": float(r.target_atr), "stop_atr": float(r.stop_atr),
                    "test": r.test, "exp_R": float(r.exp_R), "verdict": r.verdict, "trades": int(r.trades)})
    with open(os.path.join(config.STATE_DIR, "selection.json"), "w") as f:
        json.dump(sel, f, indent=2)


# ── forward paper trading ────────────────────────────────────────────────────

STATE = os.path.join(config.STATE_DIR, "live.json")
STATUS = os.path.join(config.REPORT_DIR, "live_status.json")
TRADES = os.path.join(config.REPORT_DIR, "live_trades.csv")
SENT = os.path.join(config.STATE_DIR, "alerts_sent.json")


def _start() -> pd.Timestamp:
    if os.path.isfile(STATE):
        with open(STATE) as f:
            return pd.Timestamp(json.load(f)["start"])
    t = pd.Timestamp.now(tz="UTC").floor("h")
    with open(STATE, "w") as f:
        json.dump({"start": t.isoformat()}, f)
    return t


def _watch(name: str, d: pd.DataFrame, h1: pd.DataFrame) -> tuple[float | None, float | None, str]:
    done = d.iloc[:-1] if len(d) and d.index[-1].date() >= pd.Timestamp.now(tz="UTC").date() else d
    if name.startswith("donchian"):
        n = int(name.split("_")[1])
        return float(done["High"].tail(n).max()), float(done["Low"].tail(n).min()), "daily close"
    if name == "week_break":
        w = done.resample("W-SUN").agg({"High": "max", "Low": "min"})
        if len(w) < 2:
            return None, None, ""
        return float(w["High"].iloc[-2]), float(w["Low"].iloc[-2]), "daily close"
    if name == "bos_pullback":
        from crypto_agent import levels
        piv = levels.pivots(h1.tail(24 * 60), wing=3, tf="6h")
        hs, ls = [p for p in piv if p["kind"] == "H"], [p for p in piv if p["kind"] == "L"]
        if hs and ls:
            return hs[-1]["p"], ls[-1]["p"], "6h close (then 50% pullback)"
    return None, None, ""


def live(cfg: dict | None = None, log=print) -> None:
    cfg = cfg or config.load()
    cs.MAX_HOLD_DAYS = cfg["max_hold_days"]
    start = _start()
    sel_path = os.path.join(config.STATE_DIR, "selection.json")
    if not os.path.isfile(sel_path):
        log("live: no backtest yet — run 'python -m commodity_agent backtest' first")
        return
    with open(sel_path) as f:
        sel = json.load(f)
    fx = data.fx_rate(cfg)
    status, rows, prices = [], [], {}
    frames = {}
    for key, ins in cfg["instruments"].items():
        d = data.bars(key, ins["yf"], "1d", cfg, log=log)
        h1 = data.bars(key, ins["yf"], "1h", cfg, log=log)
        frames[key] = (d, h1)
        if not h1.empty:
            prices[key] = [float(h1["Close"].iloc[-1]), str(h1.index[-1])]
    for s in sel:
        d, h1 = frames.get(s["instrument"], (pd.DataFrame(), pd.DataFrame()))
        if d.empty or h1.empty:
            continue
        af = atr_frac(d)
        sigs = [x for x in _signals(s["strategy"], d, h1) if x[0] >= start]
        tr = _run(sigs, h1, af, s["target_atr"], s["stop_atr"], cfg["cost_per_side"], 23, include_open=True)
        for t in tr:
            t.update({"instrument": s["instrument"], "strategy": s["strategy"],
                      "target_atr": s["target_atr"], "stop_atr": s["stop_atr"]})
        rows += tr
        hi, lo, basis = _watch(s["strategy"], d, h1)
        closed = [t for t in tr if t["status"] == "closed"]
        status.append({**s, "position": next((t for t in tr if t["status"] == "open"), None),
                       "watch_hi": hi, "watch_lo": lo, "watch_basis": basis,
                       "atr_pct": round(float(af.iloc[-1]) * 100, 2) if np.isfinite(af.iloc[-1]) else None,
                       "closed": len(closed), "wins": sum(1 for t in closed if t["net_pts"] > 0),
                       "total_R": round(sum(t["R"] for t in closed), 2)})
    pd.DataFrame(rows).to_csv(TRADES, index=False)
    with open(STATUS, "w") as f:
        json.dump({"start": start.isoformat(), "updated": pd.Timestamp.now(tz="UTC").isoformat(),
                   "usdinr": fx, "prices": prices, "status": status}, f, default=str)
    _alerts(status, rows, fx, log)
    log(f"live: {len(status)} strategies, {sum(1 for x in status if x['position'])} open, "
        f"{sum(x['closed'] for x in status)} closed since {start:%Y-%m-%d %H:%M} UTC")


def _alerts(status, trades, fx, log) -> None:
    from crypto_agent.notify import send
    first = not os.path.isfile(SENT)
    sent = set() if first else set(json.load(open(SENT)))
    page = os.environ.get("COMMODITY_PAGE_URL", "https://pankygr8.github.io/tradingview-mcp-jackson/commodity/")
    inr = (lambda v: f" (≈ ₹{v * fx:,.0f})") if fx else (lambda v: "")
    ist = lambda ts: pd.Timestamp(ts).tz_convert("Asia/Kolkata").strftime("%d %b %H:%M IST")
    msgs, keys = [], []
    for s in status:
        p = s.get("position")
        if not p:
            continue
        k = f"open|{s['instrument']}|{s['strategy']}|{p['entry_ts']}"
        if k in sent:
            continue
        keys.append(k)
        msgs.append(f"{'🟢' if p['side'] == 'long' else '🔴'} NEW {p['side'].upper()} — {s['instrument']} {s['strategy']}\n"
                    f"Entry {p['entry']:.2f}{inr(p['entry'])} at {ist(p['entry_ts'])}\n"
                    f"Stop {p['stop']:.2f}{inr(p['stop'])} · Target {p['target']:.2f}{inr(p['target'])}")
    for t in trades:
        if t.get("status") != "closed":
            continue
        k = f"closed|{t['instrument']}|{t['strategy']}|{t['entry_ts']}"
        if k in sent:
            continue
        keys.append(k)
        msgs.append(f"{'✅' if t['net_pts'] > 0 else '❌'} CLOSED {t['side'].upper()} — {t['instrument']} {t['strategy']}\n"
                    f"{t['entry']:.2f} → {t['exit']:.2f} ({str(t['outcome']).replace('_', ' ')}) · {t['R']:+.2f}R")
    if keys and not first:
        if not send(("🛢 Commodity swing paper trading\n\n" + "\n\n".join(msgs) + f"\n\n{page}")[:4000], log=log):
            if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                return
    with open(SENT, "w") as f:
        json.dump(sorted(sent | set(keys)), f)
