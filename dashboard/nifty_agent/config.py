"""
config.py — settings for the Nifty 200 screener agent (intraday + swing).

Override any key with nifty_agent/config.json (deep-merged).
"""

from __future__ import annotations

import copy
import json
import os
import sys

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.dirname(AGENT_DIR)
STATE_DIR = os.path.join(AGENT_DIR, "state")          # persists between runs (nifty200/ on the data branch)
DATA_DIR = os.path.join(STATE_DIR, "data")            # price cache
REPORT_DIR = os.path.join(STATE_DIR, "reports")
for _d in (STATE_DIR, DATA_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)

DEFAULTS: dict = {
    "universe": "nifty200",                    # from screener.UNIVERSES
    "benchmark": "^NSEI",

    # Costs per side (brokerage + STT + exchange + GST + stamp + slippage), as a fraction of price.
    # Intraday (MIS):  ~0.03 % charges + 0.02 % slippage  → 0.05 % per side, 0.10 % round trip
    # Delivery swing:  STT 0.1 % each side + charges + slippage → 0.13 % per side, 0.26 % round trip
    "intraday_cost_per_side": 0.0005,
    "swing_cost_per_side": 0.0013,

    # Intraday rules
    "intraday_last_entry": "14:30",            # no new entries on signal bars ending after this
    "intraday_exit": "15:15",                  # square off
    "intraday_min_stop_pct": 0.25,             # skip setups whose stop is tighter than this (% of price)
    "intraday_max_stop_pct": 3.0,              # ... or wider than this

    # Swing rules (long only — delivery)
    "swing_max_hold": 20,                      # trading days
    "swing_exits": [[1.5, 3.0], [2.0, 4.0], [2.0, 6.0]],   # [stop × ATR14, target × ATR14]

    # Verdict thresholds
    "min_trades": 30,
    "min_t_stat": 2.5,
    "min_edge_over_random": 0.10,              # R per trade above random entries with the same exits

    # Live paper trading
    "risk_per_trade_inr": 1000,                # used only to show a suggested quantity in alerts
    "live_max_candidates": 4,                  # strategies tracked live per section
}


def load() -> dict:
    path = os.path.join(AGENT_DIR, "config.json")
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def universe(cfg: dict) -> list[str]:
    if DASHBOARD_DIR not in sys.path:
        sys.path.insert(0, DASHBOARD_DIR)
    import screener                             # the same Nifty 200 list the dashboard uses
    return list(screener.UNIVERSES[cfg["universe"]])
