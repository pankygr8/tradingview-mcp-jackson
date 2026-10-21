"""
config.py — settings for the BTC / ETH intraday agent.

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
DB_PATH = os.path.join(STATE_DIR, "crypto.db")
SELECTION_PATH = os.path.join(STATE_DIR, "selection.json")

DEFAULTS: dict = {
    "coins": ["BTC", "ETH"],
    "binance_symbol": {"BTC": "BTCUSDT", "ETH": "ETHUSDT"},

    # Target: 1000 BTC points. ETH uses the SAME PERCENTAGE move:
    #   eth_target = eth_entry × 1000 / BTC price at that day's open.
    "btc_target_pts": 1000,
    "btc_stop_grid": [500, 750, 1000],          # backtest tries each; live tracks all

    # Hyperliquid perps: 0.045% taker per side, plus 0.01% assumed slippage per side
    "fee_per_side": 0.00045,
    "slippage_per_side": 0.0001,

    # Sessions are UTC (the crypto daily candle). Every trade is closed by
    # the end of its UTC day (05:30 IST next morning) at the latest.
    "backtest_months": 12,
    "live_days": 3,
    "min_trades": 30,                            # a combo needs this many trades to be ranked
    "min_t_stat": 2.0,                           # and must be statistically different from zero
    "recommend_top": 3,                          # how many combos to mark "recommended"
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
