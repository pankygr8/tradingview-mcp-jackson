"""
scan.py — run the Gann Square Blueprint on every stock with full history; save results.

state/results.json     latest scan: one row per stock (all table values + flags)
state/squares.csv      history of every PRICE-TIME SQUARE NOW signal (date, stock, details)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime

import pandas as pd

from . import blueprint, config, data

RESULTS = os.path.join(config.STATE_DIR, "results.json")
SQUARES = os.path.join(config.STATE_DIR, "squares.csv")


def run(cfg: dict, log=print) -> dict:
    uni = data.universe(cfg, log=lambda *_: None)
    names = dict(zip(uni.symbol, uni.name))
    meta = data.load_meta()
    syms = [s for s in uni.symbol if meta.get(s, {}).get("complete")]
    frames, lasts = {}, Counter()
    for s in syms:
        d = data.adjusted(s)
        if len(d) >= cfg["min_bars"]:
            frames[s] = d
            lasts[d["date"].iloc[-1]] += 1
    if not frames:
        log("scan: no stock has full history yet — the backfill is still running")
        out = {"data_date": None, "rows": [], "universe": len(uni), "ready": 0, "scanned": 0}
        _save(out)
        return out
    data_date = max(d for d, c in lasts.items() if c >= max(1, 0.2 * len(frames)))   # latest common session
    rows, stale, illiquid = [], 0, 0
    for s, d in frames.items():
        if d["date"].iloc[-1] != data_date:
            stale += 1                        # suspended / no trade today
            continue
        tail = d.tail(20)
        val = tail["value"].where(tail["value"] > 0, tail["close"] * tail["volume"])
        turnover_cr = float(val.mean()) / 1e7 if len(val) else 0.0
        if cfg["min_turnover_cr"] and turnover_cr < cfg["min_turnover_cr"]:
            illiquid += 1
            continue
        try:
            r = blueprint.evaluate(d["date"].tolist(), d["high"].values, d["low"].values, d["close"].values,
                                   ticker=s, timeframe="D", **cfg["blueprint"])
        except Exception as e:
            log(f"scan: {s} failed ({type(e).__name__}: {e})")
            continue
        if not r.get("ok"):
            continue
        prev = float(d["close"].iloc[-2]) if len(d) > 1 else r["close"]
        r.update(name=names.get(s, ""), turnover_cr=round(turnover_cr, 2),
                 chg_pct=round((r["close"] / prev - 1) * 100, 2) if prev else None)
        rows.append(r)
    out = {"data_date": data_date, "scanned_at": datetime.utcnow().isoformat() + "Z",
           "universe": len(uni), "ready": len(syms), "scanned": len(rows), "stale": stale, "illiquid": illiquid,
           "inputs": cfg["blueprint"], "rows": rows}
    _save(out)
    sq = [r for r in rows if r["square"]]
    if sq:
        hist = pd.DataFrame([{
            "date": data_date, "symbol": r["ticker"], "close": r["close"], "expect": r["expect"],
            "time_square": r["next_square"], "bars_to_square": r["bars_to_square"], "conf": r["conf"],
            "on_1x1": r["on_1x1"], "on_target": r["on_target"], "on_angle": r["on_angle"], "on_level": r["on_level"],
            "ahead": r["ahead"], "rules_work": r["rules_work"], "provisional": r["provisional"],
            "move_start": r["move_start"], "atr": r["atr"], "key": r["square_key"]} for r in sq])
        if os.path.isfile(SQUARES):
            old = pd.read_csv(SQUARES)
            hist = pd.concat([old[old.date != data_date], hist], ignore_index=True)
        hist.to_csv(SQUARES, index=False)
    log(f"scan: {data_date} — {len(rows)} stocks scanned, {len(sq)} PRICE-TIME SQUARE NOW, "
        f"{sum(r['state'] == 'due' for r in rows)} time square due (price not on a level)")
    return out


def _save(out: dict) -> None:
    with open(RESULTS, "w") as f:
        json.dump(out, f, separators=(",", ":"), default=str)


def load() -> dict:
    if os.path.isfile(RESULTS):
        with open(RESULTS) as f:
            return json.load(f)
    return {"rows": []}
