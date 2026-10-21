/**
 * main.js — Dashboard controller
 *
 * Responsibilities
 * ─────────────────
 *   • Reads / writes localStorage key "tdash" for persistence
 *   • Manages the chart-count toggle buttons (1 / 2 / 4 / 6 / 8)
 *   • Rebuilds the CSS-Grid and ChartPane set on count change
 *   • Keeps the Hyperliquid connection-status indicator updated
 */

import { ChartPane }   from './ChartPane.js';
import { dataManager } from './DataManager.js';

// ── Grid layout definitions ───────────────────────────────────────────────────

const GRID_CLASS = {
  1: 'grid-1',
  2: 'grid-2',
  4: 'grid-4',
  6: 'grid-6',
  8: 'grid-8',
};

const VALID_COUNTS = [1, 2, 4, 6, 8];

// ── Persistence helpers ───────────────────────────────────────────────────────

function loadState() {
  try { return JSON.parse(localStorage.getItem('tdash') || '{}'); }
  catch { return {}; }
}

function saveCount(n) {
  const s = loadState();
  s.chartCount = n;
  localStorage.setItem('tdash', JSON.stringify(s));
}

// ── Dashboard class ───────────────────────────────────────────────────────────

class Dashboard {
  constructor() {
    this._panes  = [];
    this._count  = this._restoredCount();
    this._grid   = document.getElementById('gridContainer');

    this._initCountBar();
    this._initStatusBadge();
    this._buildGrid(this._count);
  }

  // ── Count bar ─────────────────────────────────────────────────────────────

  _restoredCount() {
    const n = loadState().chartCount;
    return VALID_COUNTS.includes(n) ? n : 4;
  }

  _initCountBar() {
    document.querySelectorAll('.count-btn').forEach(btn => {
      const n = parseInt(btn.dataset.count, 10);
      btn.classList.toggle('active', n === this._count);
      btn.addEventListener('click', () => this._onCountClick(n));
    });
  }

  _onCountClick(n) {
    if (n === this._count) return;

    // Snapshot current pane configs so they survive the rebuild
    const snapshot = this._panes.map(p => p.getConfig());

    this._count = n;
    saveCount(n);

    // Update button highlight
    document.querySelectorAll('.count-btn').forEach(btn =>
      btn.classList.toggle('active', parseInt(btn.dataset.count, 10) === n)
    );

    this._buildGrid(n, snapshot);
  }

  // ── Grid builder ──────────────────────────────────────────────────────────

  _buildGrid(count, liveSnapshot = null) {
    // Tear down existing panes
    this._panes.forEach(p => p.destroy());
    this._panes = [];
    this._grid.innerHTML = '';

    // Apply grid class
    this._grid.className = `grid-container ${GRID_CLASS[count]}`;

    // Merge sources: live snapshot → saved localStorage → built-in defaults
    const saved = loadState().panes ?? [];

    for (let i = 0; i < count; i++) {
      const cfg = liveSnapshot?.[i] ?? saved[i] ?? null;
      this._panes.push(new ChartPane(this._grid, i, cfg));
    }
  }

  // ── Connection status badge ───────────────────────────────────────────────

  _initStatusBadge() {
    const dot  = document.querySelector('.status-dot');
    const text = document.querySelector('.status-text');

    const update = status => {
      dot.className  = `status-dot ${status}`;
      text.textContent = status === 'connected'
        ? 'Hyperliquid ✓'
        : 'Reconnecting…';
    };

    dataManager.addEventListener('hl:status', e => update(e.detail));
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  window._dash = new Dashboard();
});
