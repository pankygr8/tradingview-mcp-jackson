"""
live.py — forward paper trading of the selected intraday and swing setups.

Forward-only: the clock starts at the first live run (state/live.json). Every
run re-simulates both sections from that date on the cached prices, so a
trade's state (pending → open → closed) is simply recomputed; nothing is sent
to a broker.

Which setups are tracked comes from the last backtest (state/selection.json):
  intraday  setups positive in both halves of the 2-year 1-hour test AND positive
            in the 60-day 15-minute test, best edge over random entries first
  swing     best exit per setup that is positive in both halves of the 10-year
            test, best edge over random entries first
If none qualifies, the best two are tracked as "experimental".
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import config, data, intraday, swing

IST = "Asia/Kolkata"
START = os.path.join(config.STATE_DIR, "live.json")
SEL = os.path.join(config.STATE_DIR, "selection.json")
INTRA_TR = os.path.join(config.REPORT_DIR, "intraday_live_trades.csv")
SWING_TR = os.path.join(config.REPORT_DIR, "swing_live_trades.csv")
WATCH = os.path.join(config.REPORT_DIR, "swing_watchlist.csv")
STATUS = os.path.join(config.REPORT_DIR, "live_status.json")


def now_ist() -> pd.Timestamp:
    return pd.Timestamp.now(tz=IST)


def start() -> pd.Timestamp:
    if os.path.isfile(START):
        with open(START) as f:
            return pd.Timestamp(json.load(f)["start"])
    t = now_ist().floor("min")
    with open(START, "w") as f:
        json.dump({"start": t.isoformat()}, f)
    return t


# ── selection from backtest results ──────────────────────────────────────────
def select(intra_res: pd.DataFrame | None, swing_res: pd.DataFrame | None, cfg: dict) -> dict:
    n = cfg["live_max_candidates"]
    sel = {"intraday": [], "swing": [], "made": now_ist().isoformat()}
    ok = ("robust edge", "positive, not significant")
    if intra_res is not None and not intra_res.empty:
        h1 = intra_res[intra_res.tf == "1h"].set_index(["strategy", "target", "side"])
        m15 = intra_res[intra_res.tf == "15m"].set_index(["strategy", "target", "side"])
        rows = []
        for k, r in h1.iterrows():
            if k[2] == "both":
                continue
            r15 = m15.loc[k] if k in m15.index else None
            edge = r.exp_R - (r.random_R if pd.notna(r.random_R) else 0)
            good = r.verdict in ok and r15 is not None and r15.exp_R > 0
            rows.append((good, edge, k, r, r15))
        rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
        picked = [x for x in rows if x[0]][:n]
        if not picked:                           # nothing passed: track the two best-scoring as experimental
            enough = [x for x in rows if x[3].trades >= cfg["min_trades"]]
            picked = sorted(enough, key=lambda x: x[3].exp_R, reverse=True)[:2]
        for good, edge, k, r, r15 in picked:
            sel["intraday"].append({"strategy": k[0], "target": k[1], "side": k[2],
                                    "experimental": not good, "verdict_1h": r.verdict,
                                    "backtest_R_1h": float(r.exp_R),
                                    "backtest_R_15m": None if r15 is None else float(r15.exp_R)})
    if swing_res is not None and not swing_res.empty:
        s = swing_res.copy()
        s["edge"] = s.exp_R - s.random_R.fillna(0)
        good = s[s.verdict.isin(ok) & (s.edge > 0)].sort_values("edge", ascending=False)
        best = good.drop_duplicates("strategy").head(n)
        exp = best.empty
        if exp:
            best = s[s.trades >= cfg["min_trades"]].sort_values("exp_R", ascending=False).drop_duplicates("strategy").head(2)
        for _, r in best.iterrows():
            sa, ta = (float(x) for x in r.exit_rule.split()[0].split("/"))
            sel["swing"].append({"strategy": r.strategy, "market_filter": bool(r.market_filter), "stop_atr": sa,
                                 "target_atr": ta, "exit": r.exit_rule, "experimental": exp, "verdict": r.verdict,
                                 "backtest_R": float(r.exp_R)})
    with open(SEL, "w") as f:
        json.dump(sel, f, indent=1)
    return sel


def load_selection() -> dict:
    if os.path.isfile(SEL):
        with open(SEL) as f:
            return json.load(f)
    return {"intraday": [], "swing": []}


# ── intraday ─────────────────────────────────────────────────────────────────
def run_intraday(m15: dict, daily: dict, sel: dict, cfg: dict, t0: pd.Timestamp) -> pd.DataFrame:
    rows = []
    risk_inr = cfg["risk_per_trade_inr"]
    for sym, bars in m15.items():
        bars = data.complete_bars(bars, 15)
        bars = bars[bars.index >= t0.normalize() - pd.Timedelta(days=30)]   # 10+ sessions for the volume filter
        if bars.empty:
            continue
        st = intraday.Stock(sym, bars, daily.get(sym), 15, cfg)
        for k in range(len(st.bounds)):
            a, b = st.bounds[k]
            for c in sel["intraday"]:
                real, _ = intraday.trades_for(st, k, c["strategy"], cfg, n_avail=b - a)
                for t in real:
                    if t["target"] != c["target"] or (c["side"] != "both" and t["side"] != c["side"]):
                        continue
                    if pd.Timestamp(t["signal_ts"]) < t0:
                        continue
                    t["experimental"] = c["experimental"]
                    t["last"] = float(st.c[b - 1])
                    risk = abs(t["entry"] - t["stop"]) if pd.notna(t["entry"]) else abs(t["last"] - t["stop"])
                    t["qty"] = int(risk_inr // risk) if risk > 0 else 0
                    rows.append(t)
    tr = pd.DataFrame(rows)
    if tr.empty:
        return tr
    # at most N signals per setup per day (earliest first) — 200 stocks can fire dozens
    cap = cfg.get("intraday_max_per_day", 5)
    tr = tr.sort_values(["signal_ts", "sym"])
    tr["rank"] = tr.groupby(["strategy", "target", "date"]).cumcount()
    tr = tr[tr["rank"] < cap].drop(columns="rank")
    return tr


# ── swing ────────────────────────────────────────────────────────────────────
def run_swing(daily: dict, sel: dict, cfg: dict, t0: pd.Timestamp):
    rows, watch = [], []
    cost, hold = cfg["swing_cost_per_side"], cfg["swing_max_hold"]
    day0 = t0.tz_convert(IST).tz_localize(None).normalize()
    for sym, d in daily.items():
        if sym.startswith("^") or len(d) < 260:
            continue
        f = swing.features(d)
        A = swing.Arr(d, f)
        mkt = swing.market_ok(daily, d.index)
        start_i = int(np.searchsorted(d.index, day0))
        last_c = float(d["Close"].iloc[-1])
        for c in sel["swing"]:
            sig = swing.signals(d, f, c["strategy"])
            if c["market_filter"]:
                sig = sig & mkt
            for t in swing.run(A, sig, c["stop_atr"], c["target_atr"], cost, hold, start_i):
                t.update(sym=sym, strategy=c["strategy"], exit_rule=c["exit"], market_filter=c["market_filter"],
                         experimental=c["experimental"], last=last_c, side="long")
                if t["status"] == "pending":       # entry tomorrow at the open; show levels off today's close
                    t["stop"] = last_c - c["stop_atr"] * t["atr"]
                    t["target_px"] = last_c + c["target_atr"] * t["atr"]
                risk = (t["entry"] if pd.notna(t.get("entry")) else last_c) - t["stop"]
                t["qty"] = int(cfg["risk_per_trade_inr"] // risk) if risk > 0 else 0
                rows.append(t)
            lvl = _trigger(c["strategy"], d, f)
            if lvl and last_c < lvl <= last_c * 1.02:
                watch.append({"sym": sym, "strategy": c["strategy"], "close": last_c, "trigger": round(lvl, 2),
                              "gap%": round((lvl / last_c - 1) * 100, 2)})
    return pd.DataFrame(rows), pd.DataFrame(watch)


def _trigger(name, d, f):
    """Close needed tomorrow to fire the setup (for the 'within 2 %' watchlist)."""
    ath = float(d["High"].max())
    if name == "ath_break":
        return ath
    if name == "half_reclaim":
        return 0.5 * ath if d["Close"].iloc[-1] < 0.5 * ath else None
    if name == "donchian_55":
        return float(d["High"].tail(55).max())
    if name == "high52_volume":
        return float(d["High"].tail(252).max())
    return None


def _session_open(now: pd.Timestamp) -> bool:
    return now.weekday() < 5 and (9 * 60 + 15) <= now.hour * 60 + now.minute <= (16 * 60 + 30)


def update(cfg: dict, force: bool = False, log=print) -> None:
    sel = load_selection()
    if not sel["intraday"] and not sel["swing"]:
        log("nifty live: no selection yet — run `python -m nifty_agent backtest` first")
        return
    now = now_ist()
    t0 = start()
    syms = config.universe(cfg)
    stt = {}
    if os.path.isfile(STATUS):
        with open(STATUS) as f:
            stt = json.load(f)
    # daily bars: once after 16:00 IST (and whenever the cache is missing)
    need_daily = force or stt.get("daily_for") != str(now.date()) and (now.hour >= 16 or not stt.get("daily_for"))
    daily = data.daily(syms + [cfg["benchmark"]], refresh=need_daily, log=log)
    if need_daily and now.hour >= 16:
        stt["daily_for"] = str(now.date())
    # 15-minute bars: during the session only
    if force or _session_open(now):
        m15 = data.intraday(syms, "15m", "5d", "m15", log=log)
    else:
        m15 = data._load("m15", intraday=True)
    itr = run_intraday(m15, daily, sel, cfg, t0) if sel["intraday"] else pd.DataFrame()
    if now.weekday() < 5 and now.hour < 16:      # today's daily bar is still forming — swing uses closed days only
        today = pd.Timestamp(now.date())
        daily = {s: d[d.index < today] for s, d in daily.items()}
    str_, watch = run_swing(daily, sel, cfg, t0) if sel["swing"] else (pd.DataFrame(), pd.DataFrame())
    itr.to_csv(INTRA_TR, index=False)
    str_.drop(columns=["exit_i"], errors="ignore").to_csv(SWING_TR, index=False)
    watch.to_csv(WATCH, index=False)
    stt.update(start=t0.isoformat(), updated=now.isoformat())
    with open(STATUS, "w") as f:
        json.dump(stt, f)
    try:
        from . import notify
        notify.check(itr, str_, log=log)
    except Exception as e:                      # alerts must never break the tracker
        log(f"alerts failed: {type(e).__name__}: {e}")
    log(f"nifty live: intraday {len(itr)} trades, swing {len(str_)} trades, watchlist {len(watch)}")
