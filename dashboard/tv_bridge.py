"""
tv_bridge.py — Bridge from the Flask dashboard to TradingView Desktop.

Uses the project's existing Node.js CLI (`tv` / `src/cli/index.js`) via
subprocess so all TradingView logic stays in the JS codebase — nothing is
duplicated here.

TradingView Desktop must be running with:
    --remote-debugging-port=9222

(use scripts/launch_tv_debug.bat or the tv_launch MCP tool to start it)

NOTE: this module must contain NO Flask code. It is imported by app.py, which
owns all routes. Putting @app.route here raises NameError on import.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────

_DASHBOARD_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT   = os.path.abspath(os.path.join(_DASHBOARD_DIR, ".."))
_CLI_PATH       = os.path.join(_PROJECT_ROOT, "src", "cli", "index.js")

CDP_URL = "http://localhost:9222"
CLI_TIMEOUT = 20    # seconds per subprocess call


# ── Connectivity ──────────────────────────────────────────────────────────────

def is_tv_running() -> bool:
    """Return True if TradingView Desktop is running with CDP on port 9222."""
    try:
        resp = requests.get(f"{CDP_URL}/json/list", timeout=2)
        targets = resp.json()
        return any(
            "tradingview" in t.get("url", "").lower()
            for t in targets
            if isinstance(t, dict)
        )
    except Exception:
        return False


def get_tv_info() -> dict:
    """
    Return CDP target info (URL, title) for the active TradingView chart page.
    """
    try:
        resp = requests.get(f"{CDP_URL}/json/list", timeout=2)
        for t in resp.json():
            if isinstance(t, dict) and "tradingview" in t.get("url", "").lower():
                return {
                    "connected":   True,
                    "url":         t.get("url", ""),
                    "title":       t.get("title", ""),
                    "description": t.get("description", ""),
                }
    except Exception:
        pass
    return {"connected": False}


# ── TV CLI helper ─────────────────────────────────────────────────────────────

def _cli(*args: str) -> dict:
    """
    Run: node src/cli/index.js <args>
    Returns parsed JSON stdout on success.
    Raises RuntimeError on failure.
    """
    if not os.path.isfile(_CLI_PATH):
        raise RuntimeError(f"TV CLI not found at {_CLI_PATH}")

    cmd = ["node", _CLI_PATH, *args]
    try:
        result = subprocess.run(
            cmd,
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("TV CLI timed out (is TradingView running?)")
    except FileNotFoundError:
        raise RuntimeError("'node' not found in PATH — install Node.js")

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode == 2:
        raise RuntimeError("TradingView Desktop is not running or CDP not enabled")

    if result.returncode != 0:
        # Prefer stderr message, fall back to stdout
        msg = stderr or stdout or f"CLI exited with code {result.returncode}"
        raise RuntimeError(msg)

    if not stdout:
        return {"success": True}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected CLI output: {stdout[:200]}")


# ── Chart / studies ───────────────────────────────────────────────────────────

def get_chart_state() -> dict:
    """
    Return the current symbol, timeframe, and list of active studies
    from TradingView Desktop.
    """
    return _cli("chart", "get-state")


def get_studies() -> list[dict]:
    """
    Return the active study list [{id, name}, …] from TV Desktop.
    Gracefully returns [] if TV is not connected.
    """
    try:
        state = get_chart_state()
        return state.get("studies", [])
    except RuntimeError:
        return []


# ── Watchlist ─────────────────────────────────────────────────────────────────

def get_watchlist() -> dict:
    """Return current TV watchlist symbols."""
    return _cli("watchlist", "get")


def add_to_watchlist(symbol: str) -> dict:
    """
    Add a single symbol to the active TradingView watchlist.
    symbol should be the NSE full form, e.g. 'NSE:RELIANCE'
    """
    return _cli("watchlist", "add", symbol)


def bulk_add_to_watchlist(symbols: list[str]) -> dict:
    """
    Add multiple symbols to TV watchlist sequentially.
    Returns summary with per-symbol success/error.
    """
    results = []
    for sym in symbols:
        try:
            add_to_watchlist(sym)
            results.append({"symbol": sym, "ok": True})
            time.sleep(0.4)   # slight pause to let TV settle
        except RuntimeError as exc:
            results.append({"symbol": sym, "ok": False, "error": str(exc)})

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "success": ok_count > 0,
        "added":   ok_count,
        "failed":  len(results) - ok_count,
        "detail":  results,
    }


# ── Alerts ────────────────────────────────────────────────────────────────────

def create_alert(price: float, condition: str = "crossing", message: str = "") -> dict:
    """
    Create a price alert on the symbol CURRENTLY loaded in the TV chart.

    !! RECONSTRUCTED — the original body was overwritten. Verify the CLI's
    argument names before trusting this in a live session:
        node src/cli/index.js alert --help
    If the flags differ, fix them here only; nothing else calls the CLI directly.
    """
    args = ["alert", "create", "--price", str(price), "--condition", condition]
    if message:
        args += ["--message", message]
    return _cli(*args)


def bulk_create_alerts(alert_list: list[dict]) -> dict:
    """
    Create multiple alerts.
    Each item: {symbol, price, condition?, message?}
    NOTE: TV must be on the correct symbol to create a price alert there.
    This function switches the chart to each symbol in turn.
    """
    results = []
    for item in alert_list:
        sym   = item.get("symbol", "")
        price = item.get("price")
        cond  = item.get("condition", "crossing")
        msg   = item.get("message", f"{sym} — price alert")
        if not sym or price is None:
            results.append({"symbol": sym, "ok": False, "error": "Missing symbol or price"})
            continue
        try:
            # Switch chart to symbol first
            tv_sym = _nse_to_tv(sym)
            _cli("chart", "set-symbol", "--symbol", tv_sym)
            time.sleep(1.0)
            create_alert(float(price), cond, msg)
            results.append({"symbol": sym, "tv_symbol": tv_sym, "price": price, "ok": True})
            time.sleep(0.5)
        except RuntimeError as exc:
            results.append({"symbol": sym, "price": price, "ok": False, "error": str(exc)})
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "success": ok_count > 0,
        "created": ok_count,
        "failed":  len(results) - ok_count,
        "detail":  results,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

# Non-NSE symbol map: yfinance → TradingView.
# Futures use continuous front-month contracts (the "1!" suffix), so alerts
# survive contract rollover.
_TV_SYMBOL_MAP = {
    # NYMEX energy futures
    "CL=F": "NYMEX:CL1!",        # WTI crude
    "NG=F": "NYMEX:NG1!",        # natural gas
    # indices
    "^NSEI":    "NSE:NIFTY",
    "^BSESN":   "BSE:SENSEX",
    "^NSEBANK": "NSE:BANKNIFTY",
    # crypto — exchange prefix must be UPPERCASE, and Binance quotes in USDT,
    # not USD. Change the venue if your TV layout charts a different exchange.
    "BTC-USD": "BINANCE:BTCUSDT",
    "ETH-USD": "BINANCE:ETHUSDT",
    # forex / metals
    "EURUSD=X": "FX:EURUSD",
    "GBPUSD=X": "FX:GBPUSD",
    "AUDUSD=X": "FX:AUDUSD",
    "XAUUSD=X": "OANDA:XAUUSD",
    # NOTE: 'JPYUSD=X' is deliberately NOT mapped to 'FX:USDJPY'.
    # yfinance quotes JPY per USD (~0.0064); FX:USDJPY quotes USD per JPY (~155).
    # They are reciprocals. Mapping them would place alerts at nonsense prices.
    # If you need it, invert the price explicitly at the call site.
}


def _nse_to_tv(symbol: str) -> str:
    """
    Convert a yfinance symbol to TradingView format.
        'RELIANCE.NS' -> 'NSE:RELIANCE'
        'CL=F'        -> 'NYMEX:CL1!'
        '^NSEI'       -> 'NSE:NIFTY'

    Unknown symbols are returned unchanged, which TradingView will reject —
    that is deliberate: better a visible failure than an alert on the wrong chart.
    """
    if symbol in _TV_SYMBOL_MAP:
        return _TV_SYMBOL_MAP[symbol]
    if symbol.endswith(".NS"):
        return f"NSE:{symbol[:-3]}"
    if symbol.endswith(".BO"):
        return f"BSE:{symbol[:-3]}"
    return symbol
