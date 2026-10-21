"""
swing.py — multi-day BTC / ETH swing trades with large targets.

Why: a 1000-point intraday target pays ~85 points of costs (9 % of the target)
and nothing beat that over 3 years. Here targets are 3000 / 5000 BTC points
(ETH the same %), so the same costs are only ~2–3 % of the target.

Signals (all causal — computed on CLOSED bars, entry at the next 1-hour open;
one position at a time per strategy):
  donchian_20     daily close above the prior 20-day high → long; below the low → short
  donchian_55     same with 55 days (classic turtle breakout)
  week_break      daily close beyond the previous week's high / low
  bos_6h          6-hour close beyond the last CONFIRMED 6h swing high / low
                  (Gann swing-chart break of structure)
  bos_pullback    after a 6h break of structure, a limit order at 50 % of the
                  impulse leg (Gann 4/8) — valid 3 days
  level_1d_*      price/time levels from DAILY swings (levels.py): trendline,
                  1×1 timing line, 1.5×/2× projection — first retest after a break

Exits: fixed target, fixed stop, time exit after 10 days. Stop-first on
ambiguous bars, gaps fill at the open, fees + slippage on both sides.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import levels

TARGETS = (3000, 5000)        # BTC points
STOPS = (1500, 2500)          # BTC points
MAX_HOLD_DAYS = 10


def _h1(df5: pd.DataFrame) -> pd.DataFrame:
    return df5.resample("1h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()


def _daily(df5: pd.DataFrame) -> pd.DataFrame:
    return df5.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()


# ── signal generators: list of (time_known, side, kind, level) ───────────────
# kind "market" → enter at the next 1h open after time_known
# kind "limit"  → resting order at `level` until time_known + 3 days

def sig_donchian(d: pd.DataFrame, n: int):
    hi = d["High"].rolling(n).max().shift(1)
    lo = d["Low"].rolling(n).min().shift(1)
    out = []
    for t, c, h, l in zip(d.index, d["Close"], hi, lo):
        known = t + pd.Timedelta(days=1)                      # daily bar closes at the next midnight
        if np.isfinite(h) and c > h:
            out.append((known, "long", "market", None))
        elif np.isfinite(l) and c < l:
            out.append((known, "short", "market", None))
    return out


def sig_week_break(d: pd.DataFrame):
    w = d.resample("W-SUN").agg({"High": "max", "Low": "min"})
    prev_hi = w["High"].shift(1).reindex(d.index, method="ffill")
    prev_lo = w["Low"].shift(1).reindex(d.index, method="ffill")
    out = []
    for t, c, h, l in zip(d.index, d["Close"], prev_hi, prev_lo):
        known = t + pd.Timedelta(days=1)
        if np.isfinite(h) and c > h:
            out.append((known, "long", "market", None))
        elif np.isfinite(l) and c < l:
            out.append((known, "short", "market", None))
    return out


def sig_bos(df5: pd.DataFrame, pullback: bool):
    piv = levels.pivots(df5, wing=3, tf="6h")
    b6 = df5.resample("6h", origin="epoch").agg({"Close": "last"}).dropna()
    out = []
    last_h = last_l = None
    pi = 0
    for t, c in zip(b6.index, b6["Close"]):
        close_t = t + pd.Timedelta(hours=6)
        while pi < len(piv) and piv[pi]["conf"] <= close_t:
            if piv[pi]["kind"] == "H":
                last_h = piv[pi]
            else:
                last_l = piv[pi]
            pi += 1
        if last_h and c > last_h["p"]:
            if pullback and last_l:
                out.append((close_t, "long", "limit", (last_l["p"] + c) / 2))
            elif not pullback:
                out.append((close_t, "long", "market", None))
            last_h = None                                     # one signal per broken swing
        elif last_l and c < last_l["p"]:
            if pullback and last_h:
                out.append((close_t, "short", "limit", (last_h["p"] + c) / 2))
            elif not pullback:
                out.append((close_t, "short", "market", None))
            last_l = None
    return out


# ── execution ────────────────────────────────────────────────────────────────

def run_signals(sigs, h1: pd.DataFrame, tgt_frac: pd.Series, stop_frac: pd.Series, cost: float) -> list[dict]:
    idx = h1.index
    O, H, L, C = (h1[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    tf, sf = tgt_frac.to_numpy(float), stop_frac.to_numpy(float)
    max_bars = MAX_HOLD_DAYS * 24
    out, busy_until = [], None
    for known, side, kind, level in sorted(sigs, key=lambda s: s[0]):
        if busy_until is not None and known < busy_until:
            continue                                          # already in a trade
        i = idx.searchsorted(known)
        if i >= len(idx):
            continue
        if kind == "market":
            fill = O[i]
        else:
            end = idx.searchsorted(known + pd.Timedelta(days=3))
            hit = np.where((L[i:end] <= level) & (H[i:end] >= level))[0]
            if not len(hit):
                continue
            i += hit[0]
            fill = level if (O[i] >= level if side == "long" else O[i] <= level) else O[i]
        sgn = 1 if side == "long" else -1
        stop_pts, tgt_pts = fill * sf[i], fill * tf[i]
        stop, tgt = fill - sgn * stop_pts, fill + sgn * tgt_pts
        if kind == "limit" and ((side == "long" and L[i] <= stop) or (side == "short" and H[i] >= stop)):
            outcome, px, xi = "loss", stop, i
        else:
            outcome, px, xi = levels._exit(O, H, L, C, i if kind == "market" else i + 1, side, fill, stop, tgt, max_bars)
        if outcome == "open":
            break
        busy_until = idx[xi]
        gross = sgn * (px - fill)
        c = (fill + px) * cost
        out.append({"entry_ts": idx[i], "day": idx[i].floor("D"), "side": side, "entry": fill, "exit_ts": idx[xi],
                    "exit": px, "outcome": outcome, "status": "closed", "stop_pts": stop_pts, "target_pts": tgt_pts,
                    "gross_pts": gross, "cost_pts": c, "net_pts": gross - c, "R": (gross - c) / stop_pts,
                    "hold_d": (idx[xi] - idx[i]).total_seconds() / 86400})
    return out


def backtest(frames: dict, cfg: dict, log=print) -> tuple[pd.DataFrame, pd.DataFrame]:
    from .backtest import _stats
    cost = cfg["fee_per_side"] + cfg["slippage_per_side"]
    btc_close = _h1(frames["BTC"])["Close"]
    rows, trades = [], []
    for coin, df in frames.items():
        if df.empty:
            continue
        h1, d = _h1(df), _daily(df)
        ref = btc_close.reindex(h1.index, method="ffill").bfill()
        days = sorted(set(d.index))
        mid = days[len(days) // 2]
        span = f"{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}"
        sigsets = {
            "donchian_20": sig_donchian(d, 20),
            "donchian_55": sig_donchian(d, 55),
            "week_break": sig_week_break(d),
            "bos_6h": sig_bos(df, pullback=False),
            "bos_pullback": sig_bos(df, pullback=True),
        }
        lines = [ln for ln in levels.build_lines(levels.pivots(df, wing=2, tf="1D"))
                 if ln.family in ("trendline", "timing_1x1", "projection")]
        ref5 = btc_close.reindex(df.index, method="ffill").bfill()
        log(f"swing: {coin} {span}, signals " + ", ".join(f"{k} {len(v)}" for k, v in sigsets.items()))
        for tgt in TARGETS:
            for stp in STOPS:
                for name, sigs in sigsets.items():
                    tr = pd.DataFrame(run_signals(sigs, h1, tgt / ref, stp / ref, cost))
                    rows += _row(tr, coin, name, tgt, stp, days, mid, span, _stats)
                    if not tr.empty:
                        trades.append(tr.assign(coin=coin, strategy=name, btc_target=tgt, btc_stop=stp))
                # daily price/time levels (levels.py on DAILY swings), break-and-retest
                lt = pd.DataFrame(levels.trade_lines(lines, df, "break_retest", tgt / ref5, stp / ref5, cost,
                                                     expire_days=60, max_hold_h=MAX_HOLD_DAYS * 24, retest_h=72))
                for fam in ("trendline", "timing_1x1", "projection"):
                    t = _one_at_a_time(lt[lt.family == fam]) if not lt.empty else lt
                    rows += _row(t, coin, f"level_1d_{fam}", tgt, stp, days, mid, span, _stats)
                    if not t.empty:
                        trades.append(t.assign(coin=coin, strategy=f"level_1d_{fam}", btc_target=tgt, btc_stop=stp))
    res = pd.DataFrame(rows)
    if res.empty:
        return res, pd.DataFrame()
    ok = ((res.trades >= cfg.get("swing_min_trades", 20)) & (res.exp_R_h1 > 0) & (res.exp_R_h2 > 0)
          & (res.t_stat >= cfg.get("min_t_stat", 2.0)))
    res["verdict"] = np.where(ok, "robust edge",
                     np.where(res.trades < cfg.get("swing_min_trades", 20), "too few trades",
                     np.where((res.exp_R_h1 > 0) & (res.exp_R_h2 > 0), "positive, not significant",
                     np.where(res.exp_R > 0, "unstable (one half only)", "no edge"))))
    return res.sort_values("exp_R", ascending=False).reset_index(drop=True), \
        (pd.concat(trades) if trades else pd.DataFrame())


def _one_at_a_time(t: pd.DataFrame) -> pd.DataFrame:
    """Nearby levels often trigger on the same move; counting each as a separate
    trade inflates the sample. Keep one open position per strategy, like the
    signal-based strategies."""
    if t.empty:
        return t
    t = t.sort_values("entry_ts")
    keep, busy = [], None
    for i, (e, x) in enumerate(zip(t["entry_ts"], t["exit_ts"])):
        if busy is not None and e < busy:
            continue
        keep.append(i)
        busy = x
    return t.iloc[keep]


def _row(tr, coin, name, tgt, stp, days, mid, span, _stats):
    if tr is None or tr.empty:
        return []
    s, h1, h2 = _stats(tr), _stats(tr[tr.day < mid]), _stats(tr[tr.day >= mid])
    return [{"coin": coin, "strategy": name, "btc_target": tgt, "btc_stop": stp,
             "trades_per_month": round(s["trades"] / (len(days) / 30.4), 1), **s,
             "exp_R_h1": h1.get("exp_R"), "exp_R_h2": h2.get("exp_R"),
             "trades_h1": h1.get("trades", 0), "trades_h2": h2.get("trades", 0),
             "avg_hold_d": round(float(tr.get("hold_d", tr.get("hold_h", pd.Series([0])) / 24).mean()), 1),
             "span": span}]
