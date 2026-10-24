"""
adapters.py — run each existing scanner through its own public API and turn
its output into one common signal record.

The scanner modules are imported unchanged from the dashboard folder. Every
adapter has two halves:

  run(universe)                 -> raw payload (exactly what the dashboard gets)
  normalize(payload, ctx)       -> (records, buckets_evaluated)

Keeping them separate lets the historical replay feed payloads produced at a
past date through the very same normalisation as the live agent.

Common record
  scanner, bucket, universe, symbol, name, side (long/short), horizon
  (daily/intraday), signal_ts, session, ref_price, entry, stop, tp1, tp2,
  entry_type (market/trigger), time_budget, score, meta{}
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import config

if config.DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, config.DASHBOARD_DIR)

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now() -> datetime:
    return datetime.now(IST)


def trading_session(now: datetime | None = None) -> str:
    """The trading date a scan run 'belongs' to (weekends roll back to Friday;
    before 09:15 on a weekday means the previous session)."""
    now = now or ist_now()
    d = now.date()
    if now.weekday() < 5 and (now.hour, now.minute) < (9, 15):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return str(d)


def _wait(get_status, job_id: str, timeout: int) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = get_status(job_id)
        if st.get("status") == "done":
            return st.get("results") or {}
        if st.get("status") in ("error", "not_found"):
            raise RuntimeError(f"scan job {job_id}: {st.get('status')}")
        time.sleep(2)
    raise TimeoutError(f"scan job {job_id} did not finish in {timeout}s")


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _rec(ctx, scanner, bucket, r, side, horizon, ref_price, **kw) -> dict:
    return {
        "scanner": scanner, "bucket": bucket, "universe": ctx["universe"],
        "symbol": r.get("symbol"), "name": r.get("name"),
        "side": side, "horizon": horizon,
        "signal_ts": ctx["ts"], "session": ctx["session"],
        "ref_price": _f(ref_price),
        "entry": _f(kw.get("entry")), "stop": _f(kw.get("stop")),
        "tp1": _f(kw.get("tp1")), "tp2": _f(kw.get("tp2")),
        "entry_type": kw.get("entry_type", "market"),
        "time_budget": kw.get("time_budget"),
        "score": _f(kw.get("score")),
        "meta": kw.get("meta", {}),
    }


def _side_from_levels(entry, tp1, fallback=None):
    if entry and tp1:
        return "long" if tp1 > entry else "short"
    return fallback


# ═════════════════════════════════════════════════════════════════════════════
# 1. ATH screener (screener.py) — the ATH / 50%-of-ATH concept
# ═════════════════════════════════════════════════════════════════════════════

class ATHAdapter:
    name, horizon = "ath", "daily"

    def run(self, universe, timeout):
        import screener
        screener._cache.clear()
        jid = screener.start_scan(universe, force=True)
        return _wait(screener.get_status, jid, timeout)

    def normalize(self, payload, ctx):
        recs = []
        for r in payload.get("ath_90d", []) or []:
            recs.append(_rec(ctx, self.name, "ath_90d", r, "long", "daily", r.get("current_price"),
                             score=-(r.get("days_since_ath") or 0),
                             meta={k: r.get(k) for k in ("days_since_ath", "pct_from_ath",
                                                          "volume_ratio", "all_time_high")}))
        for r in payload.get("alert_50pct", []) or []:
            b = "half_ath_above" if r.get("above_50pct_ath") else "half_ath_below"
            recs.append(_rec(ctx, self.name, b, r, "long", "daily", r.get("current_price"),
                             score=-abs(r.get("dist_from_50pct") or 0),
                             meta={k: r.get(k) for k in ("dist_from_50pct", "half_ath_price",
                                                          "pct_from_ath", "volume_ratio",
                                                          "all_time_high")}))
        return recs, ["ath_90d", "half_ath_above", "half_ath_below"]


# ═════════════════════════════════════════════════════════════════════════════
# 2. Swing screener (swing_screener.py) — has its own entry/stop/TP/time stop
# ═════════════════════════════════════════════════════════════════════════════

class SwingAdapter:
    name, horizon = "swing", "daily"
    # time_cycle       = any setup whose bar-count from the pivot sits on a Gann cycle
    # tradeable_time   = fully tradeable setup that is ALSO on a Gann time cycle
    BUCKETS = ("tradeable", "pullback", "breakouts", "coiled", "time_cycle", "tradeable_time")

    def run(self, universe, timeout):
        import swing_screener as m
        m._cache.clear()
        jid = m.start_swing_scan(universe)
        return _wait(m.get_swing_status, jid, timeout)

    def normalize(self, payload, ctx):
        recs = []
        payload = dict(payload)
        payload["tradeable_time"] = [r for r in payload.get("tradeable", []) or [] if r.get("gann_time_cycle")]
        for b in self.BUCKETS:
            for r in payload.get(b, []) or []:
                if not r.get("entry") or not r.get("stop"):
                    continue
                ts = r.get("time_stop") or {}
                recs.append(_rec(
                    ctx, self.name, b, r, r.get("direction", "long"), "daily", r.get("close"),
                    entry=r.get("entry"), stop=r.get("stop"), tp1=r.get("tp1"), tp2=r.get("tp2"),
                    entry_type="market" if not r.get("dist_to_entry_pct") else "trigger",
                    time_budget=ts.get("budget_bars"), score=r.get("swing_score"),
                    meta={"setup": r.get("setup"), "rr": r.get("rr"), "risk_pct": r.get("risk_pct"),
                          "rs_63d": r.get("rs_63d"), "coil_ratio": r.get("coil_ratio"),
                          "rvol_daily": r.get("rvol_daily"),
                          "dist_to_entry_pct": r.get("dist_to_entry_pct"),
                          "time_cycle": bool(r.get("gann_time_cycle"))}))
        return recs, list(self.BUCKETS)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Swing-Fib-Angle signal engine (signal_engine.py) — 1d and 15m
# ═════════════════════════════════════════════════════════════════════════════

class FibAdapter:
    def __init__(self, timeframe):
        self.tf = timeframe
        self.name = f"fib_{timeframe}"
        self.horizon = "daily" if timeframe == "1d" else "intraday"

    def run(self, universe, timeout):
        import signal_engine as m
        m._cache.clear()
        jid = m.start_scan(universe, self.tf)
        return _wait(m.get_status, jid, timeout)

    def normalize(self, payload, ctx):
        recs = []
        for key, side in (("buy", "long"), ("sell", "short")):
            for r in payload.get(key, []) or []:
                s = r.get("current_signal") or {}
                if not s.get("price_still_valid", True):
                    continue
                # daily: only signals from the last 2 bars are "fresh"; the
                # engine keeps 5 bars of recency for display, which would mean
                # entering 4 days late.
                max_age = 1 if self.tf == "1d" else 4
                if (s.get("bars_ago") or 0) > max_age:
                    continue
                q = r.get("quality") or {}
                pt = (r.get("pt_square") or {}).get("score") or 0
                meta = {"bars_ago": s.get("bars_ago"), "f0125": s.get("f0125"),
                        "f0875": s.get("f0875"), "valid": s.get("valid"),
                        "trend": q.get("trend") if isinstance(q, dict) else None,
                        "pt_score": pt}
                buckets = [key] + ([f"{key}_pt"] if pt >= 60 else [])   # fib signal ON a Gann time cycle
                for b in buckets:
                    recs.append(_rec(ctx, self.name, b, r, side, self.horizon, r.get("current_price"),
                                     score=q.get("score") if isinstance(q, dict) else q, meta=meta))
        return recs, ["buy", "sell", "buy_pt", "sell_pt"]


# ═════════════════════════════════════════════════════════════════════════════
# 4. Gann fan level scanner (gann_fan_engine.py)
# ═════════════════════════════════════════════════════════════════════════════

class GannFanAdapter:
    name, horizon = "gann_fan_1d", "daily"

    def run(self, universe, timeout):
        import gann_fan_engine as m
        m._cache.clear()
        jid = m.start_scan(universe, "1d")
        return _wait(m.get_status, jid, timeout)

    def normalize(self, payload, ctx):
        recs = []
        for r in payload.get("all", []) or []:
            for field, suffix in (("signal", ""), ("confirmed_signal", "_confirmed")):
                sig = r.get(field)
                if sig not in ("BUY", "SELL"):
                    continue
                side = "long" if sig == "BUY" else "short"
                recs.append(_rec(ctx, self.name, f"{sig.lower()}{suffix}", r, side, "daily",
                                 r.get("current_price"), score=-(r.get("nearest_pct") or 0),
                                 meta={"nearest_level": r.get("nearest_level"),
                                       "nearest_pct": r.get("nearest_pct")}))
        return recs, ["buy", "sell", "buy_confirmed", "sell_confirmed"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Intraday mover screener (intraday_screener.py)
# ═════════════════════════════════════════════════════════════════════════════

class IntradayAdapter:
    name, horizon = "intraday", "intraday"

    def run(self, universe, timeout):
        import intraday_screener as m
        m._cache.clear()
        jid = m.start_intraday_scan(universe)
        return _wait(m.get_intraday_status, jid, timeout)

    def normalize(self, payload, ctx):
        recs = []
        for r in payload.get("actionable", []) or []:
            p = r.get("plan") or {}
            side = p.get("bias") if p.get("bias") in ("long", "short") else \
                _side_from_levels(p.get("entry"), p.get("tp1"))
            if not side:
                continue
            recs.append(_rec(ctx, self.name, "actionable", r, side, "intraday", r.get("current_price"),
                             entry=r.get("current_price"), stop=p.get("stop"), tp1=p.get("tp1"),
                             tp2=p.get("tp2"), entry_type="market", score=r.get("mover_score"),
                             meta={"plan_entry": p.get("entry"), "rvol": r.get("rvol"),
                                   "range_expansion": r.get("range_expansion"),
                                   "rr_tp1": p.get("rr_tp1"), "confluence": r.get("confluence"),
                                   "stale_minutes": r.get("stale_minutes")}))
        for r in payload.get("arming", []) or []:
            a = r.get("arming") or {}
            # NOTE: scan_logger.py logs plan["bias"] here, which is "none" for
            # every not-yet-broken name — so arming rows were graded as shorts.
            # The arm's own direction (or the trigger→TP1 geometry) is the truth.
            side = a.get("direction") if a.get("direction") in ("long", "short") else \
                _side_from_levels(a.get("fwd_entry") or a.get("trigger"), a.get("fwd_tp1"))
            if not side or not a.get("fwd_stop"):
                continue
            recs.append(_rec(ctx, self.name, "arming", r, side, "intraday", r.get("current_price"),
                             entry=a.get("trigger") or a.get("fwd_entry"), stop=a.get("fwd_stop"),
                             tp1=a.get("fwd_tp1"), tp2=a.get("fwd_tp2"), entry_type="trigger",
                             score=r.get("mover_score"),
                             meta={"rvol": r.get("rvol"), "range_expansion": r.get("range_expansion"),
                                   "dist_to_trigger_pct": a.get("dist_to_trigger_pct"),
                                   "stale_minutes": r.get("stale_minutes")}))
        return recs, ["actionable", "arming"]


# ═════════════════════════════════════════════════════════════════════════════
# 6. Momentum scanner (Momentum_scanner.py) — CLI module, called per symbol
# ═════════════════════════════════════════════════════════════════════════════

class MomentumAdapter:
    name, horizon = "momentum", "intraday"

    def run(self, universe, timeout):
        import Momentum_scanner as m
        from screener import UNIVERSES
        syms = UNIVERSES.get(universe) or UNIVERSES["fno"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            res = [r for r in pool.map(m.scan_symbol, syms) if r]
        return {"results": [asdict(r) for r in res]}

    def normalize(self, payload, ctx):
        from screener import ALL_SYMBOLS
        recs = []
        for r in payload.get("results", []):
            b = r.get("bucket")
            if b not in ("actionable", "arming") or r.get("direction") not in ("long", "short"):
                continue
            if not r.get("entry") or not r.get("stop"):
                continue
            # Live 21-23 Oct 2026: arming with score >= 52 -> 26 trades, 19% win, -0.30R
            # (rest of arming -0.02R). Weekly review 2026-10-24, change #1.
            if b == "arming" and (r.get("score") or 0) >= 52:
                continue
            # Actionable rows enter at the current price but keep the plan's stop; if price
            # has already run through the stop the signal is stale (graded "bad_plan").
            if b == "actionable":
                px, stop = r.get("price"), r["stop"]
                if px is None or (r["direction"] == "long" and px <= stop) or \
                        (r["direction"] == "short" and px >= stop):
                    continue
            r = {**r, "name": ALL_SYMBOLS.get(r["symbol"], r["symbol"])}
            recs.append(_rec(ctx, self.name, b, r, r["direction"], "intraday", r.get("price"),
                             entry=r["price"] if b == "actionable" else r["entry"],
                             stop=r["stop"], tp1=r.get("tp1"), tp2=r.get("tp2"),
                             entry_type="market" if b == "actionable" else "trigger",
                             score=r.get("score"),
                             meta={"plan_entry": r.get("entry"), "rvol_tod": r.get("rvol_tod"),
                                   "gap_type": r.get("gap_type"), "rr_tp1": r.get("rr_tp1"),
                                   "range_expansion": r.get("range_expansion"),
                                   "sq9_confluence": r.get("sq9_confluence")}))
        return recs, ["actionable", "arming"]


# ═════════════════════════════════════════════════════════════════════════════
# 7. Price = time square (scanner_agent/pricetime.py) — experimental
# ═════════════════════════════════════════════════════════════════════════════

class PriceTimeAdapter:
    name, horizon = "price_time", "daily"

    def run(self, universe, timeout):
        from . import pricetime
        from screener import UNIVERSES
        return pricetime.scan(UNIVERSES.get(universe) or UNIVERSES["nifty200"])

    def normalize(self, payload, ctx):
        from screener import ALL_SYMBOLS
        recs = []
        for r in payload.get("square", []) or []:
            r = {**r, "name": ALL_SYMBOLS.get(r["symbol"], r["symbol"])}
            reversal = "long" if r["move"] == "down" else "short"
            cont = "short" if reversal == "long" else "long"
            meta = {k: r.get(k) for k in ("bars_from_pivot", "units_moved", "pt_ratio", "pt_gap",
                                          "unit_pct", "move", "turned")}
            score = -r["pt_gap"]
            recs.append(_rec(ctx, self.name, "pt_reversal", r, reversal, "daily", r["close"], score=score, meta=meta))
            if r["turned"]:
                recs.append(_rec(ctx, self.name, "pt_reversal_confirmed", r, reversal, "daily", r["close"],
                                 score=score, meta=meta))
            recs.append(_rec(ctx, self.name, "pt_continuation", r, cont, "daily", r["close"], score=score, meta=meta))
        return recs, ["pt_reversal", "pt_reversal_confirmed", "pt_continuation"]


ADAPTERS = {
    "ath": ATHAdapter(),
    "swing": SwingAdapter(),
    "fib_1d": FibAdapter("1d"),
    "fib_15m": FibAdapter("15m"),
    "gann_fan_1d": GannFanAdapter(),
    "intraday": IntradayAdapter(),
    "momentum": MomentumAdapter(),
    "price_time": PriceTimeAdapter(),
}

SCANNER_LABELS = {
    "ath": "ATH / 50%-of-ATH (screener.py)",
    "swing": "Swing Gann (swing_screener.py)",
    "fib_1d": "Swing Fib Angle 1D (signal_engine.py)",
    "fib_15m": "Swing Fib Angle 15m (signal_engine.py)",
    "gann_fan_1d": "Gann Fan 1D (gann_fan_engine.py)",
    "intraday": "Intraday movers (intraday_screener.py)",
    "momentum": "Momentum (Momentum_scanner.py)",
    "price_time": "Price = Time square (pricetime.py)",
}
