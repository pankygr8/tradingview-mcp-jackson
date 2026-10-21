"""
app.py — Flask backend for the Trading Dashboard.

Original endpoints (unchanged):
    GET  /                          → index.html
    GET  /api/sources               → registered source names
    GET  /api/symbols?source=       → symbol list
    GET  /api/timeframes?source=    → timeframe list
    GET  /api/ohlcv?...             → historical bars
    GET  /api/quote?...             → latest price snapshot

New endpoints (screener + TV bridge):
    GET  /screener                            → screener.html
    GET  /api/screener/universes              → {name: stock_count}
    POST /api/screener/run  {universe}        → {job_id}
    GET  /api/screener/status/<job_id>        → {status, progress, total, results}
    GET  /api/tv/status                       → {connected, url, title}
    GET  /api/tv/studies                      → [{id, name}]
    GET  /api/tv/watchlist                    → {symbols:[…]}
    POST /api/tv/watchlist/add  {symbol}      → {success}
    POST /api/tv/watchlist/bulk {symbols:[…]} → {added, failed, detail}
    POST /api/tv/alert/create   {price,cond,msg} → {success}
    POST /api/tv/alert/bulk     [{symbol,price}] → {created, failed, detail}
    GET  /api/indicators?symbol=&timeframe=&studies= → {bars, indicators}
"""

from __future__ import annotations

import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from data_source import DATA_SOURCES, get_source
import screener as _screener
import tv_bridge as _tv
import indicators as _ind
import signal_engine as _sig
import gann_fan_engine as _gann
from intraday_screener import start_intraday_scan, get_intraday_status, gann_calculator
from swing_screener import start_swing_scan, get_swing_status

# ── App setup ─────────────────────────────────────────────────────────────────

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR  = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})


# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ── API: meta ─────────────────────────────────────────────────────────────────

@app.route("/api/sources")
def api_sources():
    return jsonify(list(DATA_SOURCES.keys()))


@app.route("/api/symbols")
def api_symbols():
    source_name = request.args.get("source", "yfinance")
    source = get_source(source_name)
    return jsonify(source.get_symbols())


@app.route("/api/timeframes")
def api_timeframes():
    source_name = request.args.get("source", "yfinance")
    source = get_source(source_name)
    return jsonify(source.get_timeframes())


# ── API: data ─────────────────────────────────────────────────────────────────

@app.route("/api/ohlcv")
def api_ohlcv():
    symbol      = request.args.get("symbol", "RELIANCE.NS")
    timeframe   = request.args.get("timeframe", "1d")
    count       = min(int(request.args.get("count", 150)), 500)
    source_name = request.args.get("source", "yfinance")

    source = get_source(source_name)
    try:
        bars = source.get_ohlcv(symbol, timeframe, count)
        return jsonify({"success": True, "symbol": symbol,
                        "timeframe": timeframe, "count": len(bars), "data": bars})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/quote")
def api_quote():
    symbol      = request.args.get("symbol", "RELIANCE.NS")
    source_name = request.args.get("source", "yfinance")

    source = get_source(source_name)
    try:
        quote = source.get_quote(symbol)
        return jsonify({"success": True, "data": quote})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# ── Screener page ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/screener")
def screener_page():
    return send_from_directory(STATIC_DIR, "screener.html")


@app.route("/api/screener/universes")
def api_screener_universes():
    return jsonify(_screener.list_universes())


@app.route("/api/screener/run", methods=["POST"])
def api_screener_run():
    body     = request.json or {}
    universe = body.get("universe", "nifty50")
    force    = bool(body.get("force", False))
    job_id   = _screener.start_scan(universe, force=force)
    return jsonify({"job_id": job_id})


@app.route("/api/screener/status/<job_id>")
def api_screener_status(job_id):
    return jsonify(_screener.get_status(job_id))

@app.route("/api/intraday/scan")
def intraday_scan():
    return {"job_id": start_intraday_scan(request.args.get("universe", "fno"))}

@app.route("/api/intraday/status/<job_id>")
def intraday_status(job_id):
    return get_intraday_status(job_id)

@app.route("/api/gann/<float:price>")
def gann(price):
    return gann_calculator(price)

@app.route("/api/swing/scan")
def swing_scan():
    return {"job_id": start_swing_scan(request.args.get("universe", "nifty500"))}

@app.route("/api/swing/status/<job_id>")
def swing_status(job_id):
    return get_swing_status(job_id)


@app.route("/gann")
def gann_board():
    return send_from_directory(app.root_path, "gann_ladder.html")

@app.route("/swing")
def swing_board():
    return send_from_directory(app.root_path, "swing_board.html")
# ═══════════════════════════════════════════════════════════════════════════════
# ── TradingView Desktop bridge ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tv/status")
def api_tv_status():
    info = _tv.get_tv_info()
    return jsonify(info)


@app.route("/api/tv/studies")
def api_tv_studies():
    try:
        studies = _tv.get_studies()
        return jsonify({"success": True, "studies": studies})
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc), "studies": []})


@app.route("/api/tv/watchlist")
def api_tv_watchlist():
    try:
        wl = _tv.get_watchlist()
        return jsonify({"success": True, **wl})
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc), "symbols": []})


@app.route("/api/tv/watchlist/add", methods=["POST"])
def api_tv_watchlist_add():
    data   = request.json or {}
    symbol = data.get("symbol", "")
    if not symbol:
        return jsonify({"success": False, "error": "symbol required"}), 400
    try:
        result = _tv.add_to_watchlist(_tv._nse_to_tv(symbol))
        return jsonify(result)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/tv/watchlist/bulk", methods=["POST"])
def api_tv_watchlist_bulk():
    data    = request.json or {}
    symbols = data.get("symbols", [])
    if not symbols:
        return jsonify({"success": False, "error": "symbols list required"}), 400
    result = _tv.bulk_add_to_watchlist([_tv._nse_to_tv(s) for s in symbols])
    return jsonify(result)


@app.route("/api/tv/alert/create", methods=["POST"])
def api_tv_alert_create():
    data  = request.json or {}
    price = data.get("price")
    if price is None:
        return jsonify({"success": False, "error": "price required"}), 400
    try:
        result = _tv.create_alert(
            float(price),
            data.get("condition", "crossing"),
            data.get("message", ""),
        )
        return jsonify(result)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/tv/alert/bulk", methods=["POST"])
def api_tv_alert_bulk():
    data       = request.json or {}
    alert_list = data.get("alerts", [])
    if not alert_list:
        return jsonify({"success": False, "error": "alerts list required"}), 400
    result = _tv.bulk_create_alerts(alert_list)
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
# ── Indicator computation ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/indicators")
def api_indicators():
    symbol    = request.args.get("symbol", "RELIANCE.NS")
    timeframe = request.args.get("timeframe", "1d")
    studies   = request.args.get("studies", "EMA20,EMA50,RSI14,MACD,BB20")
    ind_names = [s.strip() for s in studies.split(",") if s.strip()]
    try:
        result = _ind.get_for_stock(symbol, timeframe, ind_names)
        return jsonify({"success": True, **result})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# ── Swing Fib Signal Scanner ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/signals")
def signals_page():
    return send_from_directory(STATIC_DIR, "signals.html")


_ATH_UNIVERSES = frozenset({"ath_90d", "near_ath"})


@app.route("/api/signals/run", methods=["POST"])
def api_signals_run():
    body       = request.json or {}
    universe   = body.get("universe",   "nifty50")
    timeframe  = body.get("timeframe",  "1d")
    bos_mode   = body.get("bos_mode",   "Both")          # "Simple" | "Strict" | "Both"
    swing_mode = body.get("swing_mode", "Largest Range")  # "Largest Range" | "Most Recent"
    # Validate inputs to prevent cache-pollution from invalid values
    if bos_mode   not in ("Simple", "Strict", "Both"):       bos_mode   = "Both"
    if swing_mode not in ("Largest Range", "Most Recent"):    swing_mode = "Largest Range"

    # lb_bars: optional override for lookback bars (None = use TF default 200)
    lb_bars_raw = body.get("lb_bars", None)
    lb_bars: int | None = None
    if lb_bars_raw is not None:
        try:
            lb_bars = int(lb_bars_raw)
            if lb_bars < 10:   lb_bars = 10    # floor — below 10 bars is nonsense
            if lb_bars > 500:  lb_bars = 500   # cap — beyond 500 bars is too slow
        except (ValueError, TypeError):
            lb_bars = None                      # bad value → fall back to default

    # ── ATH-derived universes ────────────────────────────────────────────────
    # "ath_90d"  → stocks that hit an all-time high in the last 90 days
    # "near_ath" → stocks near 50 % of their all-time high (alert zone)
    # Both are populated from the last cached ATH screener run.
    custom_symbols: dict | None = None
    if universe in _ATH_UNIVERSES:
        ath_data = _screener.get_ath_symbols()
        syms = ath_data.get(universe, {})
        if not syms:
            return jsonify({
                "success":      False,
                "no_ath_cache": True,
                "error":        (
                    "No ATH screener results found. "
                    "Run the ATH Screener (any universe) first, then retry."
                ),
            }), 400
        custom_symbols = syms

    job_id = _sig.start_scan(universe, timeframe, bos_mode, swing_mode,
                              custom_symbols, lb_bars)
    return jsonify({"job_id": job_id})


@app.route("/api/signals/status/<job_id>")
def api_signals_status(job_id):
    return jsonify(_sig.get_status(job_id))


# ═══════════════════════════════════════════════════════════════════════════════
# ── Gann Fan Angle Scanner ───────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/gann")
def gann_fan_page():
    return send_from_directory(STATIC_DIR, "gann_fan.html")


@app.route("/api/gann/run", methods=["POST"])
def api_gann_run():
    body      = request.json or {}
    universe  = body.get("universe",  "nifty50")
    timeframe = body.get("timeframe", "1d")
    job_id    = _gann.start_scan(universe, timeframe)
    return jsonify({"job_id": job_id})


@app.route("/api/gann/status/<job_id>")
def api_gann_status(job_id):
    return jsonify(_gann.get_status(job_id))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  📊  Trading Dashboard — local server")
    print("="*55)
    print(f"  Open → http://localhost:5000")
    print(f"  Stop → Ctrl+C")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
