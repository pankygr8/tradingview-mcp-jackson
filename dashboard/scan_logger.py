"""
scan_logger.py — persist every intraday signal the screener emits.

Why this exists
───────────────
Every threshold in the screener (extension 0.35, RVOL 1.5, range 1.0, the OR
floor, Sq9 spacing) was chosen by reasoning, not measurement. This module is
the first half of closing that gap: it records what the screener flagged and
WHAT WAS KNOWN AT THE TIME, so a separate end-of-day script can score those
signals against what actually happened.

Design rules that keep the measurement honest:
  • Log only fields available at signal time. No outcome data touches this file.
  • One row per symbol per session per bucket — the FIRST time it appears.
    A name that stays actionable for an hour is one signal, logged once, at the
    moment it first qualified. Re-logging it every 3-min scan would inflate the
    hit rate with the same trade counted twenty times.
  • Append-only JSONL, one file per trading day. Survives restarts; never rewrites.

Call log_scan(payload, universe) from the scan thread once results are ready.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_logs")
_lock = threading.Lock()

# in-memory guard so we log each (session, universe, bucket, symbol) once
_seen: dict[str, set] = {}

# only these buckets represent a tradeable call worth scoring
LOGGED_BUCKETS = ("actionable", "arming")


def _session_key(universe: str) -> str:
    return f"{datetime.now(IST).date()}::{universe}"


def _logfile() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"signals_{datetime.now(IST).date()}.jsonl")


def _row(r: dict, bucket: str, universe: str) -> dict:
    """Flatten one signal to the fields knowable at signal time."""
    plan = r.get("plan", {}) or {}
    arm  = r.get("arming", {}) or {}
    now  = datetime.now(IST)

    # An arming row has not broken the opening range yet, so plan["bias"] is
    # "none" — the trade that matters is the arm's FORWARD plan. Previously the
    # bias was taken as `plan.get("bias") or arm.get("direction")`, and because
    # "none" is a truthy string every arming row was logged with bias "none"
    # (which score_signals.py then graded as a short). Use the arm's own fields.
    if bucket == "arming" and arm:
        bias  = arm.get("direction")
        entry = arm.get("fwd_entry") or arm.get("trigger")
        stop, tp1, tp2 = arm.get("fwd_stop"), arm.get("fwd_tp1"), arm.get("fwd_tp2")
        risk_pct = arm.get("fwd_risk_pct")
    else:
        bias  = plan.get("bias")
        entry, stop = plan.get("entry"), plan.get("stop")
        tp1, tp2 = plan.get("tp1"), plan.get("tp2")
        risk_pct = plan.get("risk_pct")
    if bias not in ("long", "short"):
        # last resort: read the direction off the geometry
        bias = ("long" if tp1 > entry else "short") if (entry and tp1) else "none"
    return {
        "ts_ist":       now.isoformat(timespec="seconds"),
        "session":      str(now.date()),
        "universe":     universe,
        "bucket":       bucket,
        "symbol":       r.get("symbol"),
        "name":         r.get("name"),
        # state at signal time
        "price_seen":   r.get("current_price"),
        "stale_minutes": r.get("stale_minutes"),
        "day_open":     r.get("day_open"),
        "prev_close":   r.get("prev_close"),
        "or_high":      r.get("or_high"),
        "or_low":       r.get("or_low"),
        "gann_midpoint": r.get("gann_midpoint"),
        # metrics that drove the decision
        "rvol":         r.get("rvol"),
        "gap_pct":      r.get("gap_pct"),
        "range_expansion": r.get("range_expansion"),
        "mover_score":  r.get("mover_score"),
        "trending":     r.get("trending"),
        "confluence":   r.get("confluence"),
        "time_window":  r.get("time_window"),
        # the trade the screener proposed — what we will score against reality
        "bias":         bias,
        "entry":        entry,
        "stop":         stop,
        "tp1":          tp1,
        "tp2":          tp2,
        "rr_tp1":       plan.get("rr_tp1"),
        "risk_pct":     risk_pct,
        "extension_or": plan.get("extension_or"),
        "status":       plan.get("status"),
        "tp1_feasible": plan.get("tp1_feasible"),
        # arming-specific
        "arm_trigger":  arm.get("trigger"),
        "arm_dist_pct": arm.get("dist_to_trigger_pct"),
    }


def log_scan(payload: dict, universe: str) -> int:
    """
    Append newly-seen signals from a scan payload. Returns count written.
    Safe to call on every scan; dedupe ensures one row per signal per session.
    """
    if not payload:
        return 0
    skey = _session_key(universe)
    written = 0
    with _lock:
        seen = _seen.setdefault(skey, set())
        rows = []
        for bucket in LOGGED_BUCKETS:
            for r in payload.get(bucket, []) or []:
                sym = r.get("symbol")
                if not sym:
                    continue
                mark = f"{bucket}:{sym}"
                if mark in seen:
                    continue
                seen.add(mark)
                rows.append(_row(r, bucket, universe))
        if rows:
            with open(_logfile(), "a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written = len(rows)
    return written


def todays_logfile() -> str | None:
    f = _logfile()
    return f if os.path.isfile(f) else None
