"""
config.py — commodity agent settings. Override with commodity_agent/config.json.
"""

from __future__ import annotations

import copy
import json
import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(AGENT_DIR, "state")
DATA_DIR = os.path.join(STATE_DIR, "data")
REPORT_DIR = os.path.join(STATE_DIR, "reports")

DEFAULTS: dict = {
    # key -> Yahoo Finance continuous front-month future + the MCX contract that tracks it
    "instruments": {
        "CRUDE": {"yf": "CL=F", "name": "WTI crude oil", "unit": "$/bbl", "mcx": "CRUDEOIL (₹/bbl)"},
        "NATGAS": {"yf": "NG=F", "name": "Natural gas (Henry Hub)", "unit": "$/mmBtu", "mcx": "NATURALGAS (₹/mmBtu)"},
    },
    "fx": "USDINR=X",

    # Targets and stops in multiples of the 14-day ATR (crude and gas have very
    # different volatility, so fixed points would not compare). (target, stop)
    "atr_combos": [[2.0, 1.0], [3.0, 1.0], [3.0, 1.5], [4.0, 1.5]],
    "max_hold_days": 10,

    # MCX costs: brokerage + exchange charges + CTT (0.01 % on sell) + 1–2 ticks
    # slippage ≈ 0.03 % per side
    "cost_per_side": 0.0003,

    "daily_years": 10,          # daily-bar backtest window
    "hourly_days": 729,         # Yahoo serves ~2 years of hourly bars
    "min_trades": 20,
    "min_t_stat": 2.5,          # 56 combos are tested; on pure random data t ≥ 2 flagged up to 3 by chance
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    p = os.path.join(AGENT_DIR, "config.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f))
    return copy.deepcopy(DEFAULTS)


for d in (STATE_DIR, DATA_DIR, REPORT_DIR):
    os.makedirs(d, exist_ok=True)
