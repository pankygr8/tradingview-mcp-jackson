"""
config.py — defaults for the scanner agent.

Override any key by creating scanner_agent/config.json (same shape, partial is
fine — it is deep-merged over these defaults). Nothing here needs editing for a
first run.
"""

from __future__ import annotations

import copy
import json
import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.dirname(AGENT_DIR)
DB_PATH = os.path.join(AGENT_DIR, "agent.db")
REPORT_DIR = os.path.join(AGENT_DIR, "reports")
CACHE_DIR = os.path.join(AGENT_DIR, "cache")
LOG_PATH = os.path.join(AGENT_DIR, "agent.log")

DEFAULTS: dict = {
    # ── Which scanners run, on what universe, in which schedule group ────────
    # group "daily"    → runs once after the close (and is what the replay uses)
    # group "intraday" → runs every `intraday_every_min` during market hours
    "scanners": {
        "ath":          {"enabled": True,  "group": "daily",    "universe": "nifty500"},
        "swing":        {"enabled": True,  "group": "daily",    "universe": "nifty500"},
        "fib_1d":       {"enabled": True,  "group": "daily",    "universe": "nifty200"},
        "gann_fan_1d":  {"enabled": True,  "group": "daily",    "universe": "nifty200"},
        "price_time":   {"enabled": True,  "group": "daily",    "universe": "nifty500"},
        "intraday":     {"enabled": True,  "group": "intraday", "universe": "fno"},
        "momentum":     {"enabled": True,  "group": "intraday", "universe": "fno"},
        "fib_15m":      {"enabled": False, "group": "intraday", "universe": "nifty100"},
    },

    # ── Schedule (IST) ───────────────────────────────────────────────────────
    "schedule": {
        "intraday_every_min": 15,
        "intraday_start": "09:35",     # first intraday scan (after the 15-min OR forms)
        "intraday_end":   "14:45",     # last intraday scan (later = no time to work)
        "daily_scan_at":  "15:50",     # daily scanners after the close
        "validate_at":    ["16:30", "08:45"],
        "report_at":      "16:45",
    },

    # ── Signal de-duplication ───────────────────────────────────────────────
    # A daily-scanner pick counts as a NEW signal when it enters a bucket it was
    # not in on the previous run, or when it is still there after cooldown_days.
    # Without this, a stock that sits on the ATH list for 60 days would be
    # counted as 60 trades and swamp the statistics.
    "cooldown_days": 20,

    # ── Validation ──────────────────────────────────────────────────────────
    "daily_horizons": [1, 3, 5, 10, 20],    # trading days after entry
    "primary_daily_horizon": 5,
    "intraday_horizons_min": [30, 60],      # plus end-of-day
    "benchmark": "^NSEI",

    # Uniform plan applied to EVERY signal so scanners can be compared on the
    # same trade-management rules (scanners without their own stop/target are
    # otherwise not comparable in R).
    "uniform_daily_plan":    {"stop_atr": 1.5, "target_atr": 3.0, "max_bars": 10},
    "uniform_intraday_plan": {"stop_adr_frac": 0.25, "target_r": 2.0},
    "trigger_fill_window_bars": 3,          # daily trigger entries must fill within N bars

    # ── Ranking ─────────────────────────────────────────────────────────────
    "min_signals_for_verdict": 20,
    "shrinkage_k": 30,                      # expectancy × n/(n+k)
    "tuning_min_trades": 30,                # closed trades a bucket needs before tuning hints
    "tuning_min_bin": 10,                   # minimum trades in a slice to report it

    # ── Replay (historical back-fill for daily scanners) ────────────────────
    "replay": {
        "lookback_days": 1095,              # 3 years — 1 year gave too few trades per bucket
        "step_days": 5,                     # run the scanners every N trading days
        "universe_override": "nifty100",    # smaller universe = faster replay
    },

    "max_scan_wait_sec": 1800,
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    path = os.path.join(AGENT_DIR, "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return _deep_merge(DEFAULTS, json.load(f))
    return copy.deepcopy(DEFAULTS)
