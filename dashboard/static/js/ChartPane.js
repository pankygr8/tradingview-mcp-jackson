/**
 * ChartPane.js
 *
 * One trading chart pane: market selector + symbol dropdown + TF dropdown +
 * colour-coded flashing ticker bar + Lightweight Charts candlestick +
 * volume histogram.
 *
 * Lifecycle
 * ─────────
 *   const pane = new ChartPane(gridEl, 0, savedConfig);
 *   pane.destroy();   // clean up when grid is rebuilt
 */

import { dataManager } from './DataManager.js';

// ── Symbol catalogues ─────────────────────────────────────────────────────────

const CRYPTO_SYMBOLS = [
  'BTC','ETH','SOL','BNB','XRP','ADA','AVAX','DOGE','MATIC',
  'LINK','UNI','ATOM','DOT','LTC','BCH','ARB','OP','APT',
  'SUI','FIL','INJ','TIA','SEI','WLD','BLUR','PEPE','FTM',
  'NEAR','SAND','MANA','CRV','AAVE','MKR','SNX','COMP','YFI',
];

const STOCK_SYMBOLS = [
  'RELIANCE.NS','TCS.NS','INFY.NS','HDFCBANK.NS','ICICIBANK.NS',
  'HINDUNILVR.NS','ITC.NS','SBIN.NS','BHARTIARTL.NS','BAJFINANCE.NS',
  'LT.NS','WIPRO.NS','AXISBANK.NS','ASIANPAINT.NS','MARUTI.NS',
  'TITAN.NS','SUNPHARMA.NS','ULTRACEMCO.NS','TECHM.NS','NESTLEIND.NS',
  'HCLTECH.NS','POWERGRID.NS','NTPC.NS','COALINDIA.NS','ONGC.NS',
  'ADANIENT.NS','ADANIPORTS.NS','TATAMOTORS.NS','TATASTEEL.NS',
  'JSWSTEEL.NS','BAJAJFINSV.NS','BPCL.NS','CIPLA.NS','DRREDDY.NS',
  'DIVISLAB.NS','HEROMOTOCO.NS','HINDALCO.NS','INDUSINDBK.NS',
  'M&M.NS','EICHERMOT.NS','GRASIM.NS','APOLLOHOSP.NS','BRITANNIA.NS',
  '^NSEI','^BSESN','^NSEBANK',
];

const CRYPTO_TIMEFRAMES = ['1m','5m','15m','30m','1h','4h','1d','1w'];
const STOCK_TIMEFRAMES  = ['1m','5m','15m','30m','1h','4h','1d','1w'];

/** Default config per pane slot (cycles if paneId > length) */
const DEFAULT_CONFIGS = [
  { market:'crypto', symbol:'BTC',          timeframe:'1h' },
  { market:'crypto', symbol:'ETH',          timeframe:'1h' },
  { market:'stocks', symbol:'RELIANCE.NS',  timeframe:'1d' },
  { market:'stocks', symbol:'TCS.NS',       timeframe:'1d' },
  { market:'crypto', symbol:'SOL',          timeframe:'4h' },
  { market:'stocks', symbol:'INFY.NS',      timeframe:'1d' },
  { market:'stocks', symbol:'HDFCBANK.NS',  timeframe:'1d' },
  { market:'crypto', symbol:'AVAX',         timeframe:'4h' },
];

// ── Lightweight Charts theme ──────────────────────────────────────────────────

const CHART_OPTIONS = {
  layout: {
    background: { color: '#0d1117' },
    textColor:  '#c9d1d9',
    fontSize:   11,
  },
  grid: {
    vertLines: { color: '#161b22' },
    horzLines: { color: '#161b22' },
  },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#30363d', scaleMargins: { top: 0.08, bottom: 0.22 } },
  timeScale: {
    borderColor:     '#30363d',
    timeVisible:     true,
    secondsVisible:  false,
    rightOffset:     5,
    barSpacing:      8,
  },
  handleScroll: true,
  handleScale:  true,
};

const CANDLE_OPTIONS = {
  upColor:     '#3fb950',
  downColor:   '#f85149',
  borderVisible: false,
  wickUpColor:   '#3fb950',
  wickDownColor: '#f85149',
};

const VOLUME_OPTIONS = {
  priceFormat:  { type: 'volume' },
  priceScaleId: 'vol',
};

// ── ChartPane ─────────────────────────────────────────────────────────────────

export class ChartPane {
  /**
   * @param {HTMLElement} container   Parent grid element
   * @param {number}      paneId      Zero-based index
   * @param {object|null} savedConfig Persisted config from localStorage
   */
  constructor(container, paneId, savedConfig) {
    this.id       = paneId;
    this._cfg     = this._resolveConfig(savedConfig);
    this._el      = null;
    this._chart   = null;
    this._candles = null;
    this._volume  = null;
    this._unsub   = null;    // cleanup fn from dataManager.subscribe*
    this._lastClose = null;
    this._resizeObs = null;

    this._build(container);
    this._initChart();
    this._load();
  }

  // ── Config ────────────────────────────────────────────────────────────────

  _resolveConfig(saved) {
    const def = DEFAULT_CONFIGS[this.id % DEFAULT_CONFIGS.length];
    if (!saved) return { ...def };

    // Merge saved over defaults, validate market field
    const market = saved.market === 'stocks' ? 'stocks' : 'crypto';
    const syms   = market === 'crypto' ? CRYPTO_SYMBOLS : STOCK_SYMBOLS;
    const tfs    = market === 'crypto' ? CRYPTO_TIMEFRAMES : STOCK_TIMEFRAMES;

    return {
      market,
      symbol:    syms.includes(saved.symbol)    ? saved.symbol    : syms[0],
      timeframe: tfs.includes(saved.timeframe)  ? saved.timeframe : tfs[0],
    };
  }

  _saveConfig() {
    try {
      const state = JSON.parse(localStorage.getItem('tdash') || '{}');
      if (!state.panes) state.panes = [];
      state.panes[this.id] = { ...this._cfg };
      localStorage.setItem('tdash', JSON.stringify(state));
    } catch {}
  }

  // ── DOM ───────────────────────────────────────────────────────────────────

  _build(container) {
    this._el = document.createElement('div');
    this._el.className = 'chart-pane';

    this._el.innerHTML = `
      <div class="pane-header">
        <div class="pane-controls">
          <select class="sel-market" title="Market / data source">
            <option value="crypto" ${this._cfg.market === 'crypto' ? 'selected' : ''}>Crypto</option>
            <option value="stocks" ${this._cfg.market === 'stocks' ? 'selected' : ''}>NSE Stocks</option>
          </select>
          <select class="sel-symbol" title="Symbol"></select>
          <select class="sel-tf"     title="Timeframe"></select>
        </div>
        <div class="ticker" id="ticker-${this.id}">
          <span class="t-sym">—</span>
          <span class="t-price">—</span>
          <span class="t-chg">—</span>
        </div>
      </div>
      <div class="pane-overlay" id="overlay-${this.id}">
        <div class="spinner"></div>
        <span class="overlay-msg"></span>
      </div>
      <div class="chart-wrap" id="chartwrap-${this.id}"></div>
    `;

    container.appendChild(this._el);
    this._fillSelects();
    this._bindEvents();
  }

  _fillSelects() {
    this._fillSymbolSelect();
    this._fillTfSelect();
  }

  _fillSymbolSelect() {
    const sel  = this._el.querySelector('.sel-symbol');
    const list = this._cfg.market === 'crypto' ? CRYPTO_SYMBOLS : STOCK_SYMBOLS;
    sel.innerHTML = list
      .map(s => `<option value="${s}" ${s === this._cfg.symbol ? 'selected' : ''}>${s}</option>`)
      .join('');
    if (!list.includes(this._cfg.symbol)) {
      this._cfg.symbol = list[0];
      sel.value = list[0];
    }
  }

  _fillTfSelect() {
    const sel  = this._el.querySelector('.sel-tf');
    const list = this._cfg.market === 'crypto' ? CRYPTO_TIMEFRAMES : STOCK_TIMEFRAMES;
    sel.innerHTML = list
      .map(tf => `<option value="${tf}" ${tf === this._cfg.timeframe ? 'selected' : ''}>${tf}</option>`)
      .join('');
    if (!list.includes(this._cfg.timeframe)) {
      this._cfg.timeframe = this._cfg.market === 'crypto' ? '1h' : '1d';
      sel.value = this._cfg.timeframe;
    }
  }

  _bindEvents() {
    const mktSel = this._el.querySelector('.sel-market');
    const symSel = this._el.querySelector('.sel-symbol');
    const tfSel  = this._el.querySelector('.sel-tf');

    mktSel.addEventListener('change', () => {
      this._cfg.market   = mktSel.value;
      this._cfg.symbol   = (this._cfg.market === 'crypto' ? CRYPTO_SYMBOLS : STOCK_SYMBOLS)[0];
      this._cfg.timeframe = this._cfg.market === 'crypto' ? '1h' : '1d';
      this._fillSelects();
      this._saveConfig();
      this._reload();
    });

    symSel.addEventListener('change', () => {
      const next = symSel.value;
      if (next === this._cfg.symbol) return;
      this._cfg.symbol = next;
      this._saveConfig();
      this._reload();
    });

    tfSel.addEventListener('change', () => {
      const next = tfSel.value;
      if (next === this._cfg.timeframe) return;
      this._cfg.timeframe = next;
      this._saveConfig();
      this._reload();
    });
  }

  // ── Lightweight Charts ────────────────────────────────────────────────────

  _initChart() {
    const wrap = document.getElementById(`chartwrap-${this.id}`);

    this._chart = LightweightCharts.createChart(wrap, {
      ...CHART_OPTIONS,
      width:  wrap.clientWidth  || 400,
      height: wrap.clientHeight || 300,
    });

    this._candles = this._chart.addCandlestickSeries(CANDLE_OPTIONS);

    this._volume  = this._chart.addHistogramSeries({
      ...VOLUME_OPTIONS,
      priceScaleId: 'vol',
    });
    this._chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      borderVisible: false,
    });

    // Auto-resize via ResizeObserver
    this._resizeObs = new ResizeObserver(() => {
      this._chart.applyOptions({
        width:  wrap.clientWidth,
        height: wrap.clientHeight,
      });
    });
    this._resizeObs.observe(wrap);
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  async _load() {
    this._showOverlay('loading');
    this._clearSub();

    const { market, symbol, timeframe } = this._cfg;

    try {
      let bars;

      if (market === 'crypto') {
        bars = await dataManager.fetchHL(symbol, timeframe, 300);
      } else {
        bars = await dataManager.fetchStock(symbol, timeframe, 150);
      }

      if (!bars || bars.length === 0) {
        this._showOverlay('error', 'No data returned');
        return;
      }

      // Populate chart
      this._candles.setData(bars);
      this._volume.setData(
        bars.map(b => ({
          time:  b.time,
          value: b.volume,
          color: b.close >= b.open
            ? 'rgba(63,185,80,0.45)'
            : 'rgba(248,81,73,0.45)',
        }))
      );
      this._chart.timeScale().fitContent();

      // Seed ticker from last bar
      const last = bars[bars.length - 1];
      const prev = bars.length > 1 ? bars[bars.length - 2].close : last.open;
      this._lastClose = last.close;
      this._paintTicker(last.close, prev);

      // Subscribe for live updates
      if (market === 'crypto') {
        this._unsub = dataManager.subscribeHL(symbol, timeframe, bar => this._onBar(bar));
      } else {
        this._unsub = dataManager.subscribeStock(symbol, quote => this._onQuote(quote));
      }

      this._showOverlay('hide');

    } catch (err) {
      console.error(`[Pane ${this.id}]`, err);
      this._showOverlay('error', err.message.slice(0, 80));
    }
  }

  _reload() {
    this._clearSub();
    this._load();
  }

  // ── Live update handlers ──────────────────────────────────────────────────

  /** Called for every Hyperliquid candle tick (same bar may fire many times) */
  _onBar(bar) {
    this._candles.update(bar);
    this._volume.update({
      time:  bar.time,
      value: bar.volume,
      color: bar.close >= bar.open
        ? 'rgba(63,185,80,0.45)'
        : 'rgba(248,81,73,0.45)',
    });

    if (bar.close !== this._lastClose) {
      this._paintTicker(bar.close, this._lastClose ?? bar.open);
      this._lastClose = bar.close;
    }
  }

  /** Called every 5 s for NSE stocks */
  _onQuote(quote) {
    const price = quote.price;
    if (price && price !== this._lastClose) {
      this._paintTicker(price, this._lastClose ?? price - (quote.change ?? 0));
      this._lastClose = price;
    }
  }

  // ── Ticker bar ────────────────────────────────────────────────────────────

  _paintTicker(price, prevPrice) {
    const ticker   = document.getElementById(`ticker-${this.id}`);
    if (!ticker) return;

    const symEl   = ticker.querySelector('.t-sym');
    const priceEl = ticker.querySelector('.t-price');
    const chgEl   = ticker.querySelector('.t-chg');

    const change    = price - prevPrice;
    const changePct = prevPrice ? (change / prevPrice) * 100 : 0;
    const up        = change >= 0;

    symEl.textContent   = this._displaySymbol();
    priceEl.textContent = this._fmtPrice(price);
    chgEl.textContent   =
      `${up ? '+' : ''}${change.toFixed(2)} (${up ? '+' : ''}${changePct.toFixed(2)}%)`;

    chgEl.className = `t-chg ${up ? 'up' : 'down'}`;

    // Flash — remove then re-add class to restart animation
    ticker.classList.remove('flash-up', 'flash-down');
    // Force reflow so browser actually resets the animation
    void ticker.offsetWidth;
    ticker.classList.add(up ? 'flash-up' : 'flash-down');

    // Remove flash class once animation ends (keeps DOM clean)
    ticker.addEventListener('animationend', () => {
      ticker.classList.remove('flash-up', 'flash-down');
    }, { once: true });
  }

  _displaySymbol() {
    const s = this._cfg.symbol;
    if (this._cfg.market === 'crypto') return `${s}/USDT`;
    return s.replace(/\.(NS|BO)$/i, '').replace(/^\^/, '');
  }

  _fmtPrice(p) {
    if (p >= 100_000)  return p.toLocaleString('en-IN', { maximumFractionDigits: 0 });
    if (p >= 1_000)    return p.toLocaleString('en-IN', { maximumFractionDigits: 2 });
    if (p >= 1)        return p.toFixed(4);
    return p.toFixed(6);
  }

  // ── Overlay (loading / error) ─────────────────────────────────────────────

  _showOverlay(state, msg = '') {
    const el  = document.getElementById(`overlay-${this.id}`);
    const msgEl = el?.querySelector('.overlay-msg');
    if (!el) return;

    if (state === 'hide') {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'flex';

    const spinner = el.querySelector('.spinner');
    if (state === 'loading') {
      spinner.style.display = 'block';
      if (msgEl) msgEl.textContent = '';
    } else {
      spinner.style.display = 'none';
      if (msgEl) msgEl.textContent = `⚠ ${msg}`;
    }
  }

  // ── Cleanup ───────────────────────────────────────────────────────────────

  _clearSub() {
    if (this._unsub) { this._unsub(); this._unsub = null; }
  }

  destroy() {
    this._clearSub();
    this._resizeObs?.disconnect();
    this._chart?.remove();
    this._el?.remove();
  }

  getConfig() {
    return { ...this._cfg };
  }
}
