/**
 * DataManager.js
 *
 * Single source of truth for all live market data:
 *   • Crypto  → Hyperliquid WebSocket (wss://api.hyperliquid.xyz/ws)
 *               + Hyperliquid REST for historical snapshots
 *   • Stocks  → Flask /api/ohlcv  (historical)
 *               Flask /api/quote  (live, polled every 5 s)
 *
 * Usage
 * ─────
 *   import { dataManager } from './DataManager.js';
 *
 *   // historical bars
 *   const bars = await dataManager.fetchHL('BTC', '1h');
 *   const bars = await dataManager.fetchStock('RELIANCE.NS', '1d');
 *
 *   // live candle updates (Hyperliquid)
 *   const unsub = dataManager.subscribeHL('BTC', '1h', candle => { … });
 *   unsub();   // call the returned function to cancel
 *
 *   // live quote polling (stocks)
 *   const unsub = dataManager.subscribeStock('RELIANCE.NS', quote => { … });
 *   unsub();
 */

// ── Constants ─────────────────────────────────────────────────────────────────

const HL_WS_URL   = 'wss://api.hyperliquid.xyz/ws';
const HL_REST_URL = 'https://api.hyperliquid.xyz/info';
const STOCK_POLL_INTERVAL_MS = 5_000;   // 5 s
const WS_RECONNECT_DELAY_MS  = 3_000;

/** Milliseconds per Hyperliquid interval label */
const HL_INTERVAL_MS = {
  '1m': 60e3,   '3m': 180e3,  '5m': 300e3,   '15m': 900e3,
  '30m': 1800e3, '1h': 3600e3, '2h': 7200e3,  '4h': 14400e3,
  '8h': 28800e3, '12h': 43200e3, '1d': 86400e3, '3d': 259200e3,
  '1w': 604800e3,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function hlToBars(raw) {
  return raw
    .map(c => ({
      time:   Math.floor(Number(c.t) / 1000),
      open:   parseFloat(c.o),
      high:   parseFloat(c.h),
      low:    parseFloat(c.l),
      close:  parseFloat(c.c),
      volume: parseFloat(c.v),
    }))
    .filter(c => !isNaN(c.open) && !isNaN(c.time));
}

// ── DataManager class ─────────────────────────────────────────────────────────

class DataManager extends EventTarget {
  constructor() {
    super();

    /** Map<"BTC_1h", Set<Function>>  — Hyperliquid candle subscribers */
    this._hlSubs = new Map();

    /** Map<"RELIANCE.NS", {timer, callbacks: Set<Function>}> */
    this._stockPollers = new Map();

    this._ws           = null;
    this._wsConnected  = false;
    this._wsReady      = false;   // OPEN and handshake done
    this._pendingMsgs  = [];      // queued while connecting
    this._reconnTimer  = null;
    this._destroyed    = false;

    this._openHL();
  }

  // ── Hyperliquid WebSocket ────────────────────────────────────────────────

  _openHL() {
    if (this._destroyed) return;

    this._ws = new WebSocket(HL_WS_URL);

    this._ws.addEventListener('open', () => {
      this._wsConnected = true;
      this._wsReady     = true;

      // Re-subscribe everything that was active before reconnect
      for (const key of this._hlSubs.keys()) {
        const [coin, ...rest] = key.split('_');
        const interval = rest.join('_');
        this._hlSend({ method: 'subscribe',
                        subscription: { type: 'candle', coin, interval } });
      }

      // Flush queued sends
      for (const msg of this._pendingMsgs) this._hlSend(msg);
      this._pendingMsgs = [];

      this._emit('hl:status', 'connected');
    });

    this._ws.addEventListener('message', e => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }

      if (msg.channel === 'candle' && msg.data) {
        const d   = msg.data;
        const key = `${d.s}_${d.i}`;
        const cbs = this._hlSubs.get(key);
        if (!cbs?.size) return;

        const bar = {
          time:   Math.floor(Number(d.t) / 1000),
          open:   parseFloat(d.o),
          high:   parseFloat(d.h),
          low:    parseFloat(d.l),
          close:  parseFloat(d.c),
          volume: parseFloat(d.v),
        };
        for (const cb of cbs) { try { cb(bar); } catch {} }
      }
    });

    this._ws.addEventListener('close', () => {
      this._wsConnected = false;
      this._wsReady     = false;
      this._emit('hl:status', 'disconnected');
      if (!this._destroyed) {
        this._reconnTimer = setTimeout(() => this._openHL(), WS_RECONNECT_DELAY_MS);
      }
    });

    this._ws.addEventListener('error', () => this._ws.close());
  }

  _hlSend(msg) {
    if (this._wsReady) {
      this._ws.send(JSON.stringify(msg));
    } else {
      this._pendingMsgs.push(msg);
    }
  }

  // ── Public: Hyperliquid subscriptions ────────────────────────────────────

  /**
   * Subscribe to live Hyperliquid candle updates.
   * @returns {Function} call it to unsubscribe
   */
  subscribeHL(coin, interval, callback) {
    const key = `${coin}_${interval}`;
    if (!this._hlSubs.has(key)) {
      this._hlSubs.set(key, new Set());
      this._hlSend({ method: 'subscribe',
                      subscription: { type: 'candle', coin, interval } });
    }
    this._hlSubs.get(key).add(callback);

    return () => this._unsubHL(coin, interval, callback);
  }

  _unsubHL(coin, interval, callback) {
    const key = `${coin}_${interval}`;
    const cbs = this._hlSubs.get(key);
    if (!cbs) return;
    cbs.delete(callback);
    if (cbs.size === 0) {
      this._hlSubs.delete(key);
      this._hlSend({ method: 'unsubscribe',
                      subscription: { type: 'candle', coin, interval } });
    }
  }

  // ── Public: Hyperliquid historical data ──────────────────────────────────

  async fetchHL(coin, interval, count = 300) {
    const msPerBar = HL_INTERVAL_MS[interval] ?? 86400e3;
    const endTime  = Date.now();
    const startTime = endTime - msPerBar * count;

    const resp = await fetch(HL_REST_URL, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        type: 'candleSnapshot',
        req:  { coin, interval, startTime, endTime },
      }),
    });

    if (!resp.ok) throw new Error(`Hyperliquid REST ${resp.status}`);
    const raw = await resp.json();
    if (!Array.isArray(raw)) throw new Error('Unexpected HL response');
    return hlToBars(raw);
  }

  // ── Public: Stock subscriptions (polling) ────────────────────────────────

  /**
   * Subscribe to periodic stock quote updates (every 5 s).
   * An immediate fetch is fired at subscription time so the ticker
   * shows data without waiting for the first poll interval.
   * @returns {Function} call it to unsubscribe
   */
  subscribeStock(symbol, callback) {
    if (!this._stockPollers.has(symbol)) {
      const state = { callbacks: new Set(), timer: null };
      const poll = async () => {
        try {
          const resp = await fetch(
            `/api/quote?symbol=${encodeURIComponent(symbol)}&source=yfinance`
          );
          const json = await resp.json();
          if (json.success) {
            for (const cb of state.callbacks) { try { cb(json.data); } catch {} }
          }
        } catch {}
      };
      state.timer = setInterval(poll, STOCK_POLL_INTERVAL_MS);
      this._stockPollers.set(symbol, state);
      poll(); // immediate first hit
    }

    this._stockPollers.get(symbol).callbacks.add(callback);
    return () => this._unsubStock(symbol, callback);
  }

  _unsubStock(symbol, callback) {
    const state = this._stockPollers.get(symbol);
    if (!state) return;
    state.callbacks.delete(callback);
    if (state.callbacks.size === 0) {
      clearInterval(state.timer);
      this._stockPollers.delete(symbol);
    }
  }

  // ── Public: Stock historical data ────────────────────────────────────────

  async fetchStock(symbol, timeframe, count = 150) {
    const resp = await fetch(
      `/api/ohlcv?symbol=${encodeURIComponent(symbol)}` +
      `&timeframe=${timeframe}&count=${count}&source=yfinance`
    );
    const json = await resp.json();
    if (!json.success) throw new Error(json.error || 'OHLCV fetch failed');
    return json.data ?? [];
  }

  // ── Internal event emitter ────────────────────────────────────────────────

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  destroy() {
    this._destroyed = true;
    clearTimeout(this._reconnTimer);
    this._ws?.close();
    for (const { timer } of this._stockPollers.values()) clearInterval(timer);
  }
}

// ── Singleton export ──────────────────────────────────────────────────────────

export const dataManager = new DataManager();
