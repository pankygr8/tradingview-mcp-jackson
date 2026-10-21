"""
replay.py — point-in-time historical replay of the DAILY scanners.

Waiting for live signals to accumulate takes weeks. The daily scanners
(ATH, swing, fib 1D, gann fan 1D) only read daily OHLCV through
yf.Ticker(symbol).history(...), so we can ask them "what would you have said
on date D?" by handing them a fake yfinance whose history stops at D.

  • The scanner code is NOT modified — the same _run_scan_thread() the
    dashboard uses runs here, so bucket logic is identical.
  • screener.py uses datetime.now() for "ATH in last 90 days"; that clock is
    frozen to D as well.
  • Signals are stored with source='replay' and graded by the same validator.

Known biases (stated in the report, not hidden):
  • Survivorship — the universe is TODAY's index membership.
  • yfinance auto-adjusted prices use today's corporate-action factors.
  • Intraday scanners cannot be replayed (5-min history is ~60 days, and their
    logic depends on the live clock) — they are measured forward-only.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pandas as pd

from . import adapters, config, store
from .prices import PriceCache

PERIOD_DAYS = {"max": None, "10y": 3653, "5y": 1827, "2y": 731, "1y": 366,
               "6mo": 183, "3mo": 92, "1mo": 31, "60d": 60, "5d": 7}

REPLAYABLE = ("ath", "swing", "fib_1d", "gann_fan_1d", "price_time")


class _FakeTicker:
    def __init__(self, symbol, yfake):
        self.symbol, self.y = symbol, yfake

    def history(self, period="1mo", interval="1d", start=None, end=None, **kw):
        if interval != "1d":
            return pd.DataFrame()
        df = self.y.prices.daily(self.symbol)
        if df is None:
            return pd.DataFrame()
        df = df[df.index <= self.y.asof_ts]
        days = PERIOD_DAYS.get(period)
        if days:
            df = df[df.index > self.y.asof_ts - pd.Timedelta(days=days)]
        return df.copy()


class FakeYF:
    """Stands in for the yfinance module inside the scanner modules."""

    def __init__(self, prices: PriceCache):
        self.prices = prices
        self.asof_ts = pd.Timestamp.today().normalize()

    def set_asof(self, d: date):
        self.asof_ts = pd.Timestamp(d)

    def Ticker(self, symbol):  # noqa: N802 — mirrors yfinance API
        return _FakeTicker(symbol, self)

    def download(self, *a, **k):
        return pd.DataFrame()


def _frozen_datetime(d: date):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(d.year, d.month, d.day, 10, 15, tzinfo=timezone.utc)  # 15:45 IST
            return base if tz is None else base.astimezone(tz)
    return Frozen


def _run_module_sync(scanner: str, symbols: list[str], universe: str) -> dict:
    """Call the scanner module's own background worker synchronously."""
    import uuid
    jid = str(uuid.uuid4())
    job = {"status": "pending", "progress": 0, "total": len(symbols), "results": None,
           "universe": universe, "created": time.time(), "timeframe": "1d"}
    if scanner == "ath":
        import screener as m
        m._jobs[jid] = job
        m._run_scan_thread(jid, symbols)
    elif scanner == "swing":
        import swing_screener as m
        m._jobs[jid] = job
        m._run_scan_thread(jid, symbols)
    elif scanner == "fib_1d":
        import signal_engine as m
        from screener import ALL_SYMBOLS
        m._jobs[jid] = job
        m._run_scan_thread(jid, {s: ALL_SYMBOLS.get(s, s) for s in symbols}, "1d")
    elif scanner == "gann_fan_1d":
        import gann_fan_engine as m
        from screener import ALL_SYMBOLS
        m._jobs[jid] = job
        m._run_scan_thread(jid, {s: ALL_SYMBOLS.get(s, s) for s in symbols}, "1d")
    elif scanner == "price_time":
        from . import pricetime
        return pricetime.scan(symbols)
    else:
        raise ValueError(f"{scanner} is not replayable")
    res = m._jobs[jid].get("results") or {}
    m._jobs.pop(jid, None)
    return res


def replay(scanners: list[str] | None = None, lookback_days: int | None = None,
           step_days: int | None = None, universe: str | None = None,
           fresh: bool = True, cfg: dict | None = None, prices: PriceCache | None = None,
           log=print) -> dict:
    cfg = cfg or config.load()
    rc = cfg["replay"]
    lookback_days = lookback_days or rc["lookback_days"]
    step_days = step_days or rc["step_days"]
    scanners = [s for s in (scanners or REPLAYABLE) if s in REPLAYABLE]
    prices = prices or PriceCache()
    con = store.connect()
    if fresh:
        store.clear_source(con, "replay")

    import screener
    import swing_screener
    import signal_engine
    import gann_fan_engine
    from . import pricetime
    modules = [screener, swing_screener, signal_engine, gann_fan_engine, pricetime]

    # universe per scanner (the override keeps replay time sane)
    uni_of = {s: (universe or rc.get("universe_override") or cfg["scanners"][s]["universe"])
              for s in scanners}
    all_syms = sorted({x for s in scanners for x in screener.UNIVERSES.get(uni_of[s], [])})
    log(f"replay: prefetching daily history for {len(all_syms)} symbols "
        f"(first run downloads, later runs use the cache)")
    prices.prefetch_daily(all_syms + [cfg["benchmark"]])

    bench = prices.daily(cfg["benchmark"])
    if bench is None:
        raise RuntimeError(f"no benchmark data for {cfg['benchmark']}")
    end = bench.index[-1]
    days = bench.index[bench.index >= end - pd.Timedelta(days=lookback_days)]
    asofs = [d.date() for d in days[::step_days]]
    log(f"replay: {len(asofs)} dates {asofs[0]} → {asofs[-1]}, scanners {scanners}")

    fake = FakeYF(prices)
    saved = {m: m.yf for m in modules}
    saved_dt = screener.datetime
    total_new = 0
    t0 = time.time()
    try:
        for m in modules:
            m.yf = fake
            if hasattr(m, "_cache"):
                m._cache.clear()
        for k, d in enumerate(asofs, 1):
            fake.set_asof(d)
            screener.datetime = _frozen_datetime(d)
            ctx_ts = f"{d}T15:45:00+05:30"
            for s in scanners:
                syms = screener.UNIVERSES.get(uni_of[s], [])
                payload = _run_module_sync(s, syms, uni_of[s])
                ad = adapters.ADAPTERS[s]
                recs, buckets = ad.normalize(payload, {"universe": uni_of[s], "ts": ctx_ts,
                                                       "session": str(d)})
                run_id = store.start_run(con, s, uni_of[s], "replay", str(d))
                n = store.add_signals(con, run_id, "replay", recs, buckets, s, cfg["cooldown_days"])
                store.finish_run(con, run_id, "done", len(recs), n)
                total_new += n
            if k % 5 == 0 or k == len(asofs):
                el = time.time() - t0
                log(f"replay: {k}/{len(asofs)} dates done, {total_new} signals, "
                    f"{el:.0f}s elapsed, ~{el / k * (len(asofs) - k):.0f}s left")
    finally:
        for m, y in saved.items():
            m.yf = y
            if hasattr(m, "_cache"):
                m._cache.clear()
        screener.datetime = saved_dt
    return {"dates": len(asofs), "signals": total_new}
