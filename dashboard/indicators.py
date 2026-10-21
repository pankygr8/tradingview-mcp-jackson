"""
indicators.py — Pure-pandas technical indicator calculations.

No external TA library required — all maths are standard pandas rolling/ewm ops.

Usage
─────
    from indicators import compute_indicators

    bars = [{'time':…,'open':…,'high':…,'low':…,'close':…,'volume':…}, …]
    result = compute_indicators(bars, ['EMA20','EMA50','RSI14','MACD','BB20','VWAP'])
    # result is a dict: {indicator_name → [{time, value}, …]}
"""

from __future__ import annotations

from typing import Any
import pandas as pd


# ── Core calculators ──────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).round(2)


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
          ) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_f  = _ema(series, fast)
    ema_s  = _ema(series, slow)
    macd   = ema_f - ema_s
    sig    = _ema(macd, signal)
    hist   = macd - sig
    return macd.round(4), sig.round(4), hist.round(4)


def _bollinger(series: pd.Series, period: int = 20, std: float = 2.0
               ) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid   = _sma(series, period)
    sigma = series.rolling(period).std()
    upper = (mid + std * sigma).round(4)
    lower = (mid - std * sigma).round(4)
    return upper, mid.round(4), lower


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — meaningful only for sub-daily timeframes."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum().round(4)


def _supertrend(df: pd.DataFrame, period: int = 7, multiplier: float = 3.0) -> pd.Series:
    """Supertrend direction: 1 = bullish, -1 = bearish."""
    hl2 = (df["High"] + df["Low"]) / 2
    tr  = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    ub = hl2 + multiplier * atr
    lb = hl2 - multiplier * atr

    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=float)

    for i in range(1, len(df)):
        idx = df.index[i]
        prev_idx = df.index[i - 1]
        close = df["Close"].iloc[i]
        prev_close = df["Close"].iloc[i - 1]

        # Upper band
        ub.iloc[i] = ub.iloc[i] if ub.iloc[i] < ub.iloc[i - 1] or prev_close > ub.iloc[i - 1] else ub.iloc[i - 1]
        lb.iloc[i] = lb.iloc[i] if lb.iloc[i] > lb.iloc[i - 1] or prev_close < lb.iloc[i - 1] else lb.iloc[i - 1]

        if pd.isna(st.iloc[i - 1]) or st.iloc[i - 1] == ub.iloc[i - 1]:
            st.iloc[i] = ub.iloc[i] if close <= ub.iloc[i] else lb.iloc[i]
        else:
            st.iloc[i] = lb.iloc[i] if close >= lb.iloc[i] else ub.iloc[i]

        direction.iloc[i] = -1 if st.iloc[i] == ub.iloc[i] else 1

    return direction


# ── Serialisation helper ──────────────────────────────────────────────────────

def _to_list(times: pd.Series | pd.Index, values: pd.Series) -> list[dict]:
    """Convert a time-indexed pandas Series to [{time, value}, …] dropping NaN."""
    out = []
    for t, v in zip(times, values):
        if pd.notna(v):
            out.append({"time": int(t), "value": float(v)})
    return out


def _to_triple(times, a: pd.Series, b: pd.Series, c: pd.Series,
               keys=("upper", "mid", "lower")) -> dict[str, list]:
    """Three parallel series (e.g. Bollinger Bands)."""
    result: dict[str, list] = {k: [] for k in keys}
    for t, va, vb, vc in zip(times, a, b, c):
        if pd.notna(va) and pd.notna(vb) and pd.notna(vc):
            result[keys[0]].append({"time": int(t), "value": float(va)})
            result[keys[1]].append({"time": int(t), "value": float(vb)})
            result[keys[2]].append({"time": int(t), "value": float(vc)})
    return result


# ── Public interface ──────────────────────────────────────────────────────────

# Map of known indicator names → parser config
_INDICATOR_REGISTRY: dict[str, dict] = {
    # EMA with configurable period, default 20
    **{f"EMA{p}": {"type": "ema", "period": p}     for p in [9, 20, 50, 100, 200]},
    # SMA
    **{f"SMA{p}": {"type": "sma", "period": p}     for p in [20, 50, 100, 200]},
    # RSI
    "RSI14":        {"type": "rsi",     "period": 14},
    "RSI":          {"type": "rsi",     "period": 14},
    # MACD
    "MACD":         {"type": "macd"},
    # Bollinger Bands
    "BB20":         {"type": "bb",      "period": 20, "std": 2.0},
    "BB":           {"type": "bb",      "period": 20, "std": 2.0},
    # VWAP (intraday only)
    "VWAP":         {"type": "vwap"},
    # Supertrend
    "SUPERTREND":   {"type": "supertrend", "period": 7, "mult": 3.0},
}


def compute_indicators(
    bars: list[dict],
    requested: list[str],
) -> dict[str, Any]:
    """
    Compute one or more technical indicators from a list of OHLCV bar dicts.

    Parameters
    ──────────
    bars       : OHLCV list [{time, open, high, low, close, volume}, …]
                 time = Unix seconds UTC
    requested  : indicator names, e.g. ['EMA20', 'RSI14', 'MACD', 'BB20']

    Returns
    ───────
    dict mapping indicator name → series data.
    - Simple series:  [{time, value}, …]
    - MACD:           {macd:[…], signal:[…], hist:[…]}
    - Bollinger:      {upper:[…], mid:[…], lower:[…]}
    - Supertrend:     [{time, value}] where value is 1 (bull) or −1 (bear)
    """
    if not bars:
        return {}

    df = pd.DataFrame(bars)
    df.sort_values("time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    times = df["time"]
    close = df["Close"] if "Close" in df.columns else df["close"]
    # Normalise column names
    df.columns = [c.capitalize() for c in df.columns]
    close = df["Close"]

    out: dict[str, Any] = {}

    for name in requested:
        upper_name = name.upper()
        cfg = _INDICATOR_REGISTRY.get(upper_name)
        if cfg is None:
            # Try to parse e.g. EMA(34) → EMA34
            continue

        kind = cfg["type"]

        try:
            if kind == "ema":
                out[name] = _to_list(times, _ema(close, cfg["period"]))

            elif kind == "sma":
                out[name] = _to_list(times, _sma(close, cfg["period"]))

            elif kind == "rsi":
                out[name] = _to_list(times, _rsi(close, cfg["period"]))

            elif kind == "macd":
                m, s, h = _macd(close)
                out[name] = {
                    "macd":   _to_list(times, m),
                    "signal": _to_list(times, s),
                    "hist":   _to_list(times, h),
                }

            elif kind == "bb":
                u, m, l = _bollinger(close, cfg["period"], cfg["std"])
                out[name] = _to_triple(times, u, m, l)

            elif kind == "vwap":
                if "Volume" in df.columns and "High" in df.columns:
                    out[name] = _to_list(times, _vwap(df))

            elif kind == "supertrend":
                if "High" in df.columns and "Low" in df.columns:
                    out[name] = _to_list(times, _supertrend(df, cfg["period"], cfg["mult"]))

        except Exception:
            continue   # silently skip failed indicators

    return out


def get_for_stock(symbol: str, timeframe: str, indicator_names: list[str]) -> dict:
    """
    Convenience wrapper: fetch OHLCV via yfinance and compute indicators.
    Returns {bars: […], indicators: {name: series, …}}
    """
    import yfinance as yf
    from data_source import YFinanceSource

    src  = YFinanceSource()
    bars = src.get_ohlcv(symbol, timeframe, count=300)
    inds = compute_indicators(bars, indicator_names)
    return {"symbol": symbol, "timeframe": timeframe, "bars": bars, "indicators": inds}
