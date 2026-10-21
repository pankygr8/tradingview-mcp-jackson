"""
config.py — settings for the BTC / ETH swing paper-trading agent.

Override any key with crypto_agent/config.json (deep-merged).
"""

from __future__ import annotations

import copy
import json
import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(AGENT_DIR, "state")          # everything that persists between runs
DATA_DIR = os.path.join(STATE_DIR, "data")            # 5-minute candle cache per coin
REPORT_DIR = os.path.join(STATE_DIR, "reports")

DEFAULTS: dict = {
    "coins": ["BTC", "ETH"],
    "binance_symbol": {"BTC": "BTCUSDT", "ETH": "ETHUSDT"},

    # Hyperliquid perps: 0.045% taker per side, plus 0.01% assumed slippage per side
    "fee_per_side": 0.00045,
    "slippage_per_side": 0.0001,

    "backtest_months": 36,                       # swing backtest window
    "swing_min_trades": 20,                      # a swing combo needs this many trades to be judged
    "min_t_stat": 2.0,                           # and must be statistically different from zero
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
