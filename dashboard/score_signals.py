"""
score_signals.py — grade each logged intraday signal against what actually happened.

Run after the close:  python score_signals.py [YYYY-MM-DD]   (defaults to today, IST)

What it does
────────────
For every signal in scan_logs/signals_<date>.jsonl it downloads that symbol's
real 5-minute bars for the day and asks the only question that matters:

    From the signal onward, did price reach TP1 before it hit the stop?

The result is per-signal (win / loss / open / no-fill) and aggregate: hit rate,
expectancy in R, and — the actionable part — how those numbers move as each
GATE THRESHOLD is swept. That last table is what tells you whether a threshold
is earning its place or throwing away good trades.

Honesty rules baked in
───────────────────────
  • ENTRY MODEL BY BUCKET.
      actionable → already broken; entry = the logged entry, filled at signal.
      arming     → NOT yet triggered; the trade only exists if price later
                   crosses the arm trigger. If it never does → "no_fill",
                   excluded from win/loss. Counting un-triggered arms as losses
                   (or wins) would be a lie in either direction.
  • ONLY POST-SIGNAL BARS. Outcome is judged on bars AFTER the signal timestamp.
    No bar that existed when the signal fired can affect its grade.
  • STOP-FIRST ON AMBIGUITY. If a single 5-min bar spans both stop and target,
    assume the stop hit first. Intrabar path is unknown; the conservative
    assumption keeps the hit rate honest rather than flattering.
  • yfinance intraday is ~15 min delayed and history is limited to recent days.
    Score same-day or next-day; older sessions may return no bars.

Nothing here writes to the signal log. Measurement never mutates the evidence.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_logs")


def _load(date_str: str) -> list[dict]:
    path = os.path.join(LOG_DIR, f"signals_{date_str}.jsonl")
    if not os.path.isfile(path):
        print(f"no log for {date_str} at {path}")
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _bars(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        return df.dropna(subset=["High", "Low", "Close"])
    except Exception:
        return None


def _grade(sig: dict, df: pd.DataFrame) -> dict:
    """Replay one signal against its post-signal bars."""
    entry, stop, tp1 = sig.get("entry"), sig.get("stop"), sig.get("tp1")
    bias = sig.get("bias")
    # Logs written before the scan_logger fix carry bias "none" on every arming
    # row; treating "none" as short graded every long arm backwards. Recover the
    # real direction from the trade geometry (TP1 above entry = long).
    if bias not in ("long", "short") and entry and tp1:
        bias = "long" if tp1 > entry else "short"
    if not all([entry, stop, tp1, bias]) or bias not in ("long", "short") or df is None:
        return {"outcome": "no_data"}
    # a plan whose TP1 or stop sits on the wrong side of entry is not a trade
    if (bias == "long" and not (stop < entry < tp1)) or \
       (bias == "short" and not (tp1 < entry < stop)):
        return {"outcome": "bad_plan"}

    sess = sig["session"]
    day = df[[str(ts.date()) == sess for ts in df.index]]
    if day.empty:
        return {"outcome": "no_data"}

    # bars strictly AFTER the signal timestamp (both tz-aware, IST)
    sig_ts = pd.Timestamp(sig["ts_ist"]).tz_convert(day.index.tz) \
        if pd.Timestamp(sig["ts_ist"]).tzinfo else pd.Timestamp(sig["ts_ist"]).tz_localize(IST).tz_convert(day.index.tz)
    fwd = day[day.index > sig_ts]
    if fwd.empty:
        return {"outcome": "no_bars_after"}

    long = bias == "long"
    filled = sig["bucket"] == "actionable"   # actionable is entered at signal
    fill_price = entry

    for ts, bar in fwd.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])

        # arming: the trade only begins if price crosses the trigger. On the bar
        # that triggers, we fill AT the trigger and only judge stop/target on the
        # rest of that bar's excursion — the move BEFORE the trigger isn't ours.
        if not filled:
            trig = sig.get("arm_trigger") or entry
            if (long and hi >= trig) or (not long and lo <= trig):
                filled = True
                fill_price = trig
                # clip this bar's range to the side of the trigger we entered on
                if long:
                    lo = max(lo, trig)      # can't have been stopped below entry pre-fill
                else:
                    hi = min(hi, trig)
            else:
                continue

        hit_stop = (lo <= stop) if long else (hi >= stop)
        hit_tp1  = (hi >= tp1) if long else (lo <= tp1)

        if hit_stop and hit_tp1:
            # ambiguous bar → assume stop first (conservative)
            return {"outcome": "loss", "exit": stop, "fill": fill_price,
                    "r": _r(fill_price, stop, stop, long)}
        if hit_stop:
            return {"outcome": "loss", "exit": stop, "fill": fill_price,
                    "r": _r(fill_price, stop, stop, long)}
        if hit_tp1:
            return {"outcome": "win", "exit": tp1, "fill": fill_price,
                    "r": _r(fill_price, stop, tp1, long)}

    if not filled:
        return {"outcome": "no_fill"}     # arm never triggered → not a trade
    # ran to the close without hitting either — mark to last price
    last = float(fwd["Close"].iloc[-1])
    return {"outcome": "open", "exit": round(last, 2), "fill": fill_price,
            "r": _r(fill_price, stop, last, long)}


def _r(entry, stop, exit_, long) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    move = (exit_ - entry) if long else (entry - exit_)
    return round(move / risk, 2)


def _summary(graded: list[dict], label: str) -> None:
    filled = [g for g in graded if g["grade"]["outcome"] in ("win", "loss", "open")]
    wins  = [g for g in filled if g["grade"]["outcome"] == "win"]
    losses = [g for g in filled if g["grade"]["outcome"] == "loss"]
    nofill = [g for g in graded if g["grade"]["outcome"] == "no_fill"]
    rs = [g["grade"]["r"] for g in filled if "r" in g["grade"]]
    exp = sum(rs) / len(rs) if rs else 0.0
    hit = len(wins) / len(filled) * 100 if filled else 0.0
    print(f"\n── {label} ──")
    print(f"  signals {len(graded)}  |  filled {len(filled)}  |  arm no-fill {len(nofill)}")
    if filled:
        print(f"  hit rate (reached TP1 before stop): {hit:.0f}%  "
              f"({len(wins)}W / {len(losses)}L / {len(filled)-len(wins)-len(losses)} open)")
        print(f"  expectancy: {exp:+.2f} R per filled signal")


def _sweep(graded: list[dict], field: str, thresholds: list[float]) -> None:
    """Show how hit-rate / expectancy change if a gate had been stricter."""
    print(f"\n── threshold sweep on '{field}' ──")
    print(f"  {'min '+field:>16} {'signals':>8} {'filled':>7} {'hit%':>6} {'exp R':>7}")
    for th in thresholds:
        sub = [g for g in graded if (g["sig"].get(field) or 0) >= th]
        filled = [g for g in sub if g["grade"]["outcome"] in ("win", "loss", "open")]
        wins = [g for g in filled if g["grade"]["outcome"] == "win"]
        rs = [g["grade"]["r"] for g in filled if "r" in g["grade"]]
        hit = len(wins)/len(filled)*100 if filled else 0
        exp = sum(rs)/len(rs) if rs else 0
        print(f"  {th:>16.2f} {len(sub):>8} {len(filled):>7} {hit:>5.0f}% {exp:>+6.2f}")


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else str(datetime.now(IST).date())
    signals = _load(date_str)
    if not signals:
        return
    print(f"scoring {len(signals)} signals from {date_str}")

    # group bar downloads by symbol (one fetch per symbol, not per signal)
    by_symbol = defaultdict(list)
    for s in signals:
        by_symbol[s["symbol"]].append(s)

    graded = []
    for sym, sigs in by_symbol.items():
        df = _bars(sym)
        for s in sigs:
            graded.append({"sig": s, "grade": _grade(s, df)})

    # overall + per bucket
    _summary(graded, "ALL SIGNALS")
    for bucket in ("actionable", "arming"):
        sub = [g for g in graded if g["sig"]["bucket"] == bucket]
        if sub:
            _summary(sub, bucket.upper())

    # sensitivity: are the gates earning their keep?
    act = [g for g in graded if g["sig"]["bucket"] == "actionable"]
    if act:
        _sweep(act, "rvol", [1.0, 1.5, 2.0, 2.5, 3.0])
        _sweep(act, "range_expansion", [1.0, 1.25, 1.5, 2.0])
        _sweep(act, "mover_score", [0, 5, 10, 15])

    # write a flat CSV for your own analysis
    rows = []
    for g in graded:
        row = dict(g["sig"]); row.update({"outcome": g["grade"]["outcome"],
                                          "result_r": g["grade"].get("r"),
                                          "exit": g["grade"].get("exit")})
        rows.append(row)
    out = os.path.join(LOG_DIR, f"scored_{date_str}.csv")
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nper-signal detail → {out}")
    print("\nNOTE: small samples are noise. A threshold that looks best over 20\n"
          "signals may reverse over 200. Accumulate weeks before trusting the sweep.")


if __name__ == "__main__":
    main()
