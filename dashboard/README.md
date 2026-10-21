# Trading Dashboard

Responsive multi-chart dashboard with live crypto and Indian-stock data,
running entirely on `localhost`. No cloud deployment required.

## Quick start

**Windows:**
```bat
cd dashboard
run.bat
```

**Mac / Linux:**
```bash
cd dashboard
chmod +x run.sh && ./run.sh
```

Then open **http://localhost:5000** in your browser.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.9+ | https://www.python.org/downloads/ — tick "Add to PATH" on Windows |
| Internet    | Needed for Hyperliquid WS + yfinance market data |

Python packages are installed automatically by the launcher scripts.

---

## Features

| Feature | Detail |
|---------|--------|
| **Chart count** | 1 / 2 / 4 / 6 / 8 — remembered across reloads |
| **Grid layouts** | 1×1, 2×1, 2×2, 3×2, 4×2 (cleanest fit per count) |
| **Per-pane symbol** | Independent dropdown — crypto or NSE stock |
| **Per-pane timeframe** | Independent dropdown — 1m … 1w |
| **Live crypto** | Hyperliquid WebSocket (direct from browser, zero latency) |
| **Live NSE stocks** | yfinance polled every 5 s through Flask |
| **Ticker bar** | Flashes green ↑ / red ↓ on every price tick |
| **Volume histogram** | Colour-coded bars below each candlestick chart |

---

## Architecture

```
Browser
│
├─ Hyperliquid WebSocket  wss://api.hyperliquid.xyz/ws   (direct)
│   └─ candle ticks → DataManager → ChartPane.update()
│
└─ Flask  http://localhost:5000
    ├─ GET /api/ohlcv    → yfinance historical bars
    └─ GET /api/quote    → yfinance latest price (polled every 5 s)
```

**Key files:**

| File | Purpose |
|------|---------|
| `data_source.py` | Abstract `DataSource` class + `YFinanceSource` implementation |
| `app.py` | Flask REST API (4 endpoints, threaded, CORS-enabled) |
| `static/js/DataManager.js` | WS hub + poll manager; singleton shared by all panes |
| `static/js/ChartPane.js` | One pane: chart, dropdowns, ticker, subscription lifecycle |
| `static/js/main.js` | Grid builder, count selector, localStorage persistence |
| `static/style.css` | Dark theme, CSS Grid layouts, flash animations |

---

## Adding a new data source (broker / exchange)

1. Open `data_source.py`.
2. Subclass `DataSource` and implement three methods:

```python
class DhanSource(DataSource):
    SYMBOLS    = [...]
    TIMEFRAMES = ['1m','5m','15m','1h','1d']

    def get_ohlcv(self, symbol, timeframe, count=150) -> list[dict]:
        # Each dict: {time, open, high, low, close, volume}
        # time = Unix seconds (UTC)
        ...

    def get_quote(self, symbol) -> dict:
        # Must contain: symbol, price, change, change_pct, time
        ...

    def get_symbols(self) -> list[str]: return self.SYMBOLS
    def get_timeframes(self) -> list[str]: return self.TIMEFRAMES
```

3. Register it:
```python
DATA_SOURCES = {
    "yfinance": YFinanceSource(),
    "dhan":     DhanSource(),     # ← add here
}
```

4. The Flask endpoints and frontend market dropdown pick it up automatically via
   `/api/symbols?source=dhan` and `/api/ohlcv?source=dhan&symbol=...`.

---

## Crypto symbols (Hyperliquid)

Handled entirely in the browser — no backend changes needed for new coins.
Add to `CRYPTO_SYMBOLS` in `static/js/ChartPane.js`.

Supported timeframes: `1m 5m 15m 30m 1h 4h 1d 1w`

---

## Notes

- yfinance data has a ~15-20 min delay during NSE market hours.
- 1-min intraday data is only available for the last 7 calendar days (yfinance limit).
- Hyperliquid perpetuals only (no spot). Symbol names: `BTC`, `ETH`, `SOL` …
