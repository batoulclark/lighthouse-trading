/**
 * @fileoverview Price Alert System — Lighthouse Trading Dashboard
 *
 * A self-contained ES module for managing price alerts on trading pairs.
 * Alerts are persisted in localStorage and checked against live fill_price
 * data from the /dashboard/trades endpoint.
 *
 * @module price-alerts
 *
 * Public API:
 *   renderPriceAlerts(containerId)  — Mount the alert UI into a DOM element
 *   checkAlerts(trades)             — Evaluate alerts against an array of trades
 *
 * @version 1.0.0
 */

'use strict';

/* ═══════════════════════════════════════════════════════════════════════════ */
/* CONSTANTS                                                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

/** localStorage key used to persist alerts across sessions. */
const STORAGE_KEY = 'lh_price_alerts';

/** Trading pairs supported by the alert picker. */
const SUPPORTED_PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'ADAUSDT'];

/** Alert direction types. */
const ALERT_TYPES = [
  { value: 'above', label: 'Goes Above' },
  { value: 'below', label: 'Goes Below' },
  { value: 'cross', label: 'Crosses'    },
];

/** How long (ms) a triggered alert banner stays visible before auto-dismiss. */
const BANNER_DURATION_MS = 8000;

/** Beep frequency and duration for the optional sound notification. */
const SOUND_FREQ_HZ  = 880;
const SOUND_DURATION = 0.35; // seconds

/* ═══════════════════════════════════════════════════════════════════════════ */
/* INTERNAL STATE                                                             */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Module-level state object — not exported; updated internally only.
 * @type {{ alerts: Alert[], soundEnabled: boolean, lastPrices: Record<string,number> }}
 */
const _state = {
  /** @type {Alert[]} */
  alerts: [],

  /** Whether the user has opted into sound notifications. */
  soundEnabled: false,

  /**
   * Tracks the most recent fill_price seen per pair so that "cross" type
   * alerts can detect direction change between polling cycles.
   * @type {Record<string, number>}
   */
  lastPrices: {},

  /**
   * Set of container element IDs that have been mounted.
   * @type {Set<string>}
   */
  mountedContainers: new Set(),
};

/* ═══════════════════════════════════════════════════════════════════════════ */
/* TYPES (JSDoc)                                                              */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * @typedef {Object} Alert
 * @property {string}  id          - Unique identifier (UUID-like)
 * @property {string}  pair        - Trading pair symbol, e.g. "BTCUSDT"
 * @property {number}  price       - Target price level
 * @property {'above'|'below'|'cross'} type - Alert trigger direction
 * @property {boolean} triggered   - Whether this alert has already fired
 * @property {string}  createdAt   - ISO timestamp when alert was created
 * @property {string|null} triggeredAt - ISO timestamp when it fired (null until then)
 */

/**
 * @typedef {Object} Trade
 * @property {string}  pair        - Trading pair symbol
 * @property {number}  fill_price  - Execution price
 * @property {string}  timestamp   - ISO timestamp of the trade
 * @property {string}  [bot_name]  - Optional bot identifier
 * @property {string}  [action]    - "buy" | "sell"
 */

/* ═══════════════════════════════════════════════════════════════════════════ */
/* PERSISTENCE                                                                */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Loads alert state from localStorage into `_state.alerts`.
 * Safe to call at any time; handles parse errors gracefully.
 */
function _loadAlerts() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    _state.alerts = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(_state.alerts)) _state.alerts = [];
  } catch {
    _state.alerts = [];
  }
}

/**
 * Persists current `_state.alerts` to localStorage.
 */
function _saveAlerts() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(_state.alerts));
  } catch (e) {
    console.warn('[price-alerts] localStorage write failed:', e);
  }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* ALERT CRUD                                                                 */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Generates a simple unique ID string.
 * @returns {string}
 */
function _uid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

/**
 * Creates and stores a new alert.
 * @param {string}  pair   - Trading pair symbol
 * @param {number}  price  - Target price level
 * @param {'above'|'below'|'cross'} type - Alert type
 * @returns {Alert} The newly created alert
 */
function _addAlert(pair, price, type) {
  /** @type {Alert} */
  const alert = {
    id:          _uid(),
    pair:        pair.toUpperCase(),
    price:       Number(price),
    type,
    triggered:   false,
    createdAt:   new Date().toISOString(),
    triggeredAt: null,
  };
  _state.alerts.unshift(alert);
  _saveAlerts();
  return alert;
}

/**
 * Removes an alert by ID.
 * @param {string} id - Alert ID to delete
 */
function _deleteAlert(id) {
  _state.alerts = _state.alerts.filter(a => a.id !== id);
  _saveAlerts();
}

/**
 * Marks an alert as triggered and records when it fired.
 * @param {string} id         - Alert ID
 * @param {string} [timestamp] - ISO timestamp; defaults to now
 */
function _markTriggered(id, timestamp = new Date().toISOString()) {
  const alert = _state.alerts.find(a => a.id === id);
  if (alert) {
    alert.triggered   = true;
    alert.triggeredAt = timestamp;
    _saveAlerts();
  }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* TRIGGER LOGIC                                                              */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Evaluates whether a single alert should fire based on the current price
 * for that pair. Handles cross-detection by comparing against the last seen
 * price stored in `_state.lastPrices`.
 *
 * @param {Alert}  alert        - The alert to evaluate
 * @param {number} currentPrice - Latest fill_price for alert.pair
 * @returns {boolean} True if the alert should trigger now
 */
function _shouldTrigger(alert, currentPrice) {
  if (alert.triggered) return false;

  const target = alert.price;

  switch (alert.type) {
    case 'above':
      return currentPrice > target;

    case 'below':
      return currentPrice < target;

    case 'cross': {
      const prev = _state.lastPrices[alert.pair];
      if (prev == null) return false; // Need at least two data points
      // Crossed from below to above or from above to below
      const crossedUp   = prev <= target && currentPrice > target;
      const crossedDown = prev >= target && currentPrice < target;
      return crossedUp || crossedDown;
    }

    default:
      return false;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* NOTIFICATIONS                                                              */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Plays a short alert beep using the Web Audio API.
 * Silent if AudioContext is unavailable or the user hasn't interacted yet.
 */
function _playBeep() {
  try {
    const ctx  = new (window.AudioContext || window.webkitAudioContext)();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.type      = 'sine';
    osc.frequency.setValueAtTime(SOUND_FREQ_HZ, ctx.currentTime);
    gain.gain.setValueAtTime(0.25, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + SOUND_DURATION);

    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + SOUND_DURATION);

    osc.onended = () => ctx.close();
  } catch {
    // AudioContext not available — silently skip
  }
}

/**
 * Displays a dismissible notification banner for a triggered alert.
 * Automatically removes itself after BANNER_DURATION_MS.
 *
 * @param {Alert}  alert        - The alert that triggered
 * @param {number} currentPrice - The price that caused the trigger
 */
function _showBanner(alert, currentPrice) {
  // Remove any existing banner for the same alert
  const existing = document.getElementById('lh-alert-banner-' + alert.id);
  if (existing) existing.remove();

  const banner = document.createElement('div');
  banner.id = 'lh-alert-banner-' + alert.id;

  Object.assign(banner.style, {
    position:      'fixed',
    top:           '72px',
    right:         '20px',
    zIndex:        '9998',
    background:    'linear-gradient(135deg, #1e3a5f, #1e4d2b)',
    border:        '1px solid #22c55e',
    borderLeft:    '4px solid #22c55e',
    borderRadius:  '10px',
    padding:       '14px 18px',
    minWidth:      '280px',
    maxWidth:      '360px',
    boxShadow:     '0 8px 32px rgba(0,0,0,.4)',
    fontFamily:    "'Inter', -apple-system, sans-serif",
    color:         '#e2e8f0',
    cursor:        'pointer',
    animation:     'lhAlertSlideIn .3s ease',
    userSelect:    'none',
  });

  const typeLabel = ALERT_TYPES.find(t => t.value === alert.type)?.label ?? alert.type;
  const priceFmt  = _formatPrice(currentPrice);
  const targetFmt = _formatPrice(alert.price);

  banner.innerHTML = `
    <div style="display:flex;align-items:flex-start;gap:10px">
      <span style="font-size:1.3rem;line-height:1">🔔</span>
      <div style="flex:1;min-width:0">
        <div style="font-weight:700;font-size:.95rem;margin-bottom:3px;color:#86efac">
          ${_esc(alert.pair)} Alert Triggered
        </div>
        <div style="font-size:.82rem;color:#94a3b8;line-height:1.45">
          Price <strong style="color:#e2e8f0">${priceFmt}</strong>
          ${typeLabel.toLowerCase()} target
          <strong style="color:#fbbf24">${targetFmt}</strong>
        </div>
        <div style="font-size:.7rem;color:#64748b;margin-top:4px">
          ${new Date().toLocaleTimeString()}
        </div>
      </div>
      <button onclick="this.closest('[id^=lh-alert-banner]').remove()"
              style="background:none;border:none;color:#64748b;font-size:1.1rem;cursor:pointer;padding:0;line-height:1">✕</button>
    </div>
  `;

  // Inject slide-in animation once
  if (!document.getElementById('lh-alert-styles')) {
    const style = document.createElement('style');
    style.id = 'lh-alert-styles';
    style.textContent = `
      @keyframes lhAlertSlideIn {
        from { opacity: 0; transform: translateX(20px); }
        to   { opacity: 1; transform: translateX(0); }
      }
      @keyframes lhAlertFadeOut {
        to { opacity: 0; transform: translateX(20px); }
      }
    `;
    document.head.appendChild(style);
  }

  document.body.appendChild(banner);

  // Auto-dismiss
  const timer = setTimeout(() => {
    banner.style.animation = 'lhAlertFadeOut .4s ease forwards';
    setTimeout(() => banner.remove(), 400);
  }, BANNER_DURATION_MS);

  // Click to dismiss early
  banner.addEventListener('click', () => {
    clearTimeout(timer);
    banner.remove();
  });
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* UTILITIES                                                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * XSS-safe string escaping.
 * @param {*} s
 * @returns {string}
 */
function _esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Formats a price number for display (auto-precision based on magnitude).
 * @param {number} price
 * @returns {string}
 */
function _formatPrice(price) {
  if (price == null || isNaN(price)) return '—';
  const n = Number(price);
  if (n >= 10000) return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 100)   return '$' + n.toFixed(2);
  if (n >= 1)     return '$' + n.toFixed(4);
  return '$' + n.toFixed(6);
}

/**
 * Returns a human-readable label for an alert type.
 * @param {'above'|'below'|'cross'} type
 * @returns {string}
 */
function _typeLabel(type) {
  return ALERT_TYPES.find(t => t.value === type)?.label ?? type;
}

/**
 * Returns a CSS color string for a given alert type.
 * @param {'above'|'below'|'cross'} type
 * @returns {string}
 */
function _typeColor(type) {
  if (type === 'above') return '#22c55e';
  if (type === 'below') return '#ef4444';
  return '#3b82f6';
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* RENDER — UI COMPONENTS                                                     */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Generates the HTML string for the "add alert" form.
 * @returns {string}
 */
function _renderForm() {
  const pairOptions = SUPPORTED_PAIRS
    .map(p => `<option value="${p}">${p}</option>`)
    .join('');

  const typeOptions = ALERT_TYPES
    .map(t => `<option value="${t.value}">${t.label}</option>`)
    .join('');

  return `
    <div class="lh-pa-form" style="
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 12px 16px;
      background: rgba(15,23,42,.04);
      border-bottom: 1px solid rgba(15,23,42,.08);
    ">
      <select id="lh-pa-pair" title="Trading pair" style="
        background: #fff;
        border: 1px solid rgba(15,23,42,.15);
        border-radius: 6px;
        padding: 6px 10px;
        font-size: .8rem;
        font-family: inherit;
        color: #0f172a;
        cursor: pointer;
        min-width: 110px;
      ">
        ${pairOptions}
      </select>

      <input
        id="lh-pa-price"
        type="number"
        step="any"
        min="0"
        placeholder="Price…"
        title="Target price"
        style="
          background: #fff;
          border: 1px solid rgba(15,23,42,.15);
          border-radius: 6px;
          padding: 6px 10px;
          font-size: .8rem;
          font-family: inherit;
          color: #0f172a;
          width: 130px;
        "
      />

      <select id="lh-pa-type" title="Alert type" style="
        background: #fff;
        border: 1px solid rgba(15,23,42,.15);
        border-radius: 6px;
        padding: 6px 10px;
        font-size: .8rem;
        font-family: inherit;
        color: #0f172a;
        cursor: pointer;
        min-width: 120px;
      ">
        ${typeOptions}
      </select>

      <button
        id="lh-pa-add-btn"
        title="Add alert"
        style="
          background: linear-gradient(135deg, #3b82f6, #8b5cf6);
          border: none;
          border-radius: 6px;
          padding: 6px 16px;
          font-size: .8rem;
          font-weight: 700;
          font-family: inherit;
          color: #fff;
          cursor: pointer;
          white-space: nowrap;
          transition: opacity .15s;
        "
        onmouseenter="this.style.opacity='.85'"
        onmouseleave="this.style.opacity='1'"
      >
        + Add Alert
      </button>

      <label style="
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: .75rem;
        color: #64748b;
        cursor: pointer;
        margin-left: auto;
        white-space: nowrap;
      " title="Enable sound notification when alert triggers">
        <input id="lh-pa-sound" type="checkbox" style="accent-color:#3b82f6" />
        🔔 Sound
      </label>
    </div>
  `;
}

/**
 * Generates the HTML for a single alert row in the list.
 * @param {Alert} alert
 * @returns {string}
 */
function _renderAlertRow(alert) {
  const color       = _typeColor(alert.type);
  const typeLabel   = _typeLabel(alert.type);
  const targetFmt   = _formatPrice(alert.price);
  const triggeredCls = alert.triggered ? 'opacity:.45;' : '';
  const statusText  = alert.triggered
    ? `✓ Triggered at ${new Date(alert.triggeredAt).toLocaleTimeString()}`
    : 'Waiting…';

  return `
    <div
      id="lh-pa-row-${_esc(alert.id)}"
      style="
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 9px 16px;
        border-bottom: 1px solid rgba(15,23,42,.06);
        font-size: .8rem;
        ${triggeredCls}
        transition: background .15s;
      "
      onmouseenter="this.style.background='rgba(59,130,246,.04)'"
      onmouseleave="this.style.background=''"
    >
      <!-- Color dot for type -->
      <span style="
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
        background: ${color};
        box-shadow: 0 0 5px ${color}88;
      "></span>

      <!-- Pair badge -->
      <span style="
        font-weight: 700;
        color: #3b82f6;
        min-width: 80px;
        font-size: .78rem;
      ">${_esc(alert.pair)}</span>

      <!-- Type + target -->
      <span style="color:#475569;flex:1;min-width:0">
        <span style="color:${color};font-weight:600">${_esc(typeLabel)}</span>
        <span style="color:#94a3b8"> · </span>
        <span style="font-weight:700;color:#0f172a">${targetFmt}</span>
      </span>

      <!-- Status -->
      <span style="
        font-size: .7rem;
        color: ${alert.triggered ? '#22c55e' : '#94a3b8'};
        min-width: 120px;
        text-align: right;
      ">${statusText}</span>

      <!-- Delete -->
      <button
        title="Delete alert"
        data-alert-id="${_esc(alert.id)}"
        class="lh-pa-delete-btn"
        style="
          background: none;
          border: none;
          color: #94a3b8;
          font-size: .85rem;
          cursor: pointer;
          padding: 2px 4px;
          border-radius: 4px;
          line-height: 1;
          transition: color .15s, background .15s;
          flex-shrink: 0;
        "
        onmouseenter="this.style.color='#ef4444';this.style.background='rgba(239,68,68,.08)'"
        onmouseleave="this.style.color='#94a3b8';this.style.background=''"
      >✕</button>
    </div>
  `;
}

/**
 * Renders the complete alerts list section.
 * @returns {string}
 */
function _renderAlertList() {
  if (_state.alerts.length === 0) {
    return `
      <div style="
        text-align: center;
        color: #94a3b8;
        padding: 28px 20px;
        font-size: .8rem;
      ">
        No alerts set — add one above.
      </div>
    `;
  }

  // Active first, triggered after
  const sorted = [..._state.alerts].sort((a, b) => {
    if (a.triggered !== b.triggered) return a.triggered ? 1 : -1;
    return new Date(b.createdAt) - new Date(a.createdAt);
  });

  return `
    <div style="max-height: 320px; overflow-y: auto;">
      ${sorted.map(_renderAlertRow).join('')}
    </div>
  `;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* FULL WIDGET RENDER + MOUNT                                                 */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * (Re-)renders the entire widget HTML into a mounted container.
 * @param {HTMLElement} container
 */
function _renderWidget(container) {
  const activeCount    = _state.alerts.filter(a => !a.triggered).length;
  const triggeredCount = _state.alerts.filter(a =>  a.triggered).length;

  container.innerHTML = `
    <div style="
      background: #ffffff;
      border: 1px solid rgba(15,23,42,.1);
      border-radius: 12px;
      box-shadow: 0 1px 3px rgba(0,0,0,.1), 0 4px 12px rgba(0,0,0,.06);
      overflow: hidden;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    ">
      <!-- Header -->
      <div style="
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border-bottom: 1px solid rgba(15,23,42,.08);
        background: rgba(15,23,42,.02);
      ">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:1rem">🔔</span>
          <span style="font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#64748b">
            Price Alerts
          </span>
          ${activeCount > 0 ? `
          <span style="
            background: rgba(59,130,246,.12);
            color: #3b82f6;
            border-radius: 8px;
            padding: 2px 8px;
            font-size: .68rem;
            font-weight: 700;
          ">${activeCount} active</span>
          ` : ''}
          ${triggeredCount > 0 ? `
          <span style="
            background: rgba(34,197,94,.12);
            color: #22c55e;
            border-radius: 8px;
            padding: 2px 8px;
            font-size: .68rem;
            font-weight: 700;
          ">${triggeredCount} triggered</span>
          ` : ''}
        </div>

        <!-- Clear triggered button -->
        ${triggeredCount > 0 ? `
        <button
          id="lh-pa-clear-triggered"
          title="Remove triggered alerts"
          style="
            background: none;
            border: 1px solid rgba(15,23,42,.12);
            border-radius: 6px;
            padding: 3px 10px;
            font-size: .7rem;
            font-weight: 600;
            font-family: inherit;
            color: #64748b;
            cursor: pointer;
            transition: all .15s;
          "
          onmouseenter="this.style.borderColor='#ef4444';this.style.color='#ef4444'"
          onmouseleave="this.style.borderColor='rgba(15,23,42,.12)';this.style.color='#64748b'"
        >Clear triggered</button>
        ` : ''}
      </div>

      <!-- Add Alert Form -->
      ${_renderForm()}

      <!-- Alert List -->
      <div id="lh-pa-list">
        ${_renderAlertList()}
      </div>
    </div>
  `;

  _bindEvents(container);
}

/**
 * Binds DOM event listeners after render. Uses event delegation on container.
 * @param {HTMLElement} container
 */
function _bindEvents(container) {
  // Sync sound checkbox state
  const soundCb = container.querySelector('#lh-pa-sound');
  if (soundCb) soundCb.checked = _state.soundEnabled;

  // Add alert button
  const addBtn = container.querySelector('#lh-pa-add-btn');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      const pairEl  = container.querySelector('#lh-pa-pair');
      const priceEl = container.querySelector('#lh-pa-price');
      const typeEl  = container.querySelector('#lh-pa-type');

      const pair  = pairEl?.value?.trim();
      const price = parseFloat(priceEl?.value);
      const type  = typeEl?.value;

      if (!pair) {
        _flashInput(pairEl, 'Select a pair');
        return;
      }
      if (!price || price <= 0 || isNaN(price)) {
        _flashInput(priceEl, 'Enter a valid price');
        return;
      }
      if (!type) {
        _flashInput(typeEl, 'Select a type');
        return;
      }

      _addAlert(pair, price, type);
      if (priceEl) priceEl.value = '';
      _refreshList(container);
      _refreshHeader(container);
    });
  }

  // Sound toggle
  if (soundCb) {
    soundCb.addEventListener('change', () => {
      _state.soundEnabled = soundCb.checked;
    });
  }

  // Price input — allow Enter key
  const priceEl = container.querySelector('#lh-pa-price');
  if (priceEl) {
    priceEl.addEventListener('keydown', e => {
      if (e.key === 'Enter') addBtn?.click();
    });
  }

  // Event delegation for delete and clear buttons
  container.addEventListener('click', e => {
    // Delete individual alert
    const deleteBtn = e.target.closest('.lh-pa-delete-btn');
    if (deleteBtn) {
      const id = deleteBtn.dataset.alertId;
      if (id) {
        _deleteAlert(id);
        _refreshList(container);
        _refreshHeader(container);
      }
      return;
    }

    // Clear all triggered
    if (e.target.closest('#lh-pa-clear-triggered')) {
      _state.alerts = _state.alerts.filter(a => !a.triggered);
      _saveAlerts();
      _renderWidget(container); // full re-render to update header badges
    }
  });
}

/**
 * Flashes a brief error highlight on an input to guide the user.
 * @param {HTMLElement|null} el
 * @param {string} msg
 */
function _flashInput(el, msg) {
  if (!el) return;
  const orig = el.style.borderColor;
  el.style.borderColor = '#ef4444';
  el.style.boxShadow   = '0 0 0 2px rgba(239,68,68,.25)';
  el.title = msg;
  setTimeout(() => {
    el.style.borderColor = orig;
    el.style.boxShadow   = '';
    el.title = '';
  }, 1800);
}

/**
 * Re-renders only the list section (not the whole widget).
 * More efficient than a full re-render for add/delete operations.
 * @param {HTMLElement} container
 */
function _refreshList(container) {
  const listEl = container.querySelector('#lh-pa-list');
  if (listEl) listEl.innerHTML = _renderAlertList();

  // Re-bind delete buttons (they're inside listEl)
  // Already handled by container-level delegation — no extra binding needed.
}

/**
 * Refreshes just the header badge counts.
 * @param {HTMLElement} container
 */
function _refreshHeader(container) {
  // Simplest reliable approach: re-render the whole widget.
  // The list is small so perf is fine.
  _renderWidget(container);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* PUBLIC API                                                                 */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Mounts the Price Alerts widget into a DOM element.
 *
 * Creates the full UI (form + list) inside the specified container.
 * Safe to call multiple times with different container IDs — each gets its
 * own independent render but shares the same underlying alert state.
 *
 * @param {string} containerId - The `id` of the DOM element to mount into
 * @returns {void}
 *
 * @example
 * // In dashboard.html:
 * import { renderPriceAlerts } from './components/price-alerts.js';
 * renderPriceAlerts('my-alerts-panel');
 */
export function renderPriceAlerts(containerId) {
  if (!containerId || typeof containerId !== 'string') {
    console.error('[price-alerts] renderPriceAlerts: containerId must be a non-empty string');
    return;
  }

  const container = document.getElementById(containerId);
  if (!container) {
    console.error(`[price-alerts] renderPriceAlerts: element #${containerId} not found`);
    return;
  }

  // Load persisted alerts on first mount
  _loadAlerts();

  _renderWidget(container);
  _state.mountedContainers.add(containerId);
}

/**
 * Evaluates all active (non-triggered) alerts against the latest trades.
 *
 * This function is designed to be called on every data refresh cycle, passing
 * the freshly fetched array of trades from `/dashboard/trades`. It extracts
 * the most recent `fill_price` per trading pair and tests each alert.
 *
 * When an alert triggers:
 * 1. The alert is marked as triggered in state + localStorage
 * 2. A visual banner notification is shown
 * 3. An optional sound beep plays (if user enabled it)
 * 4. All mounted widgets are re-rendered to reflect the new state
 *
 * @param {Trade[]} trades - Array of trade objects from `/dashboard/trades`
 * @returns {{ triggered: Alert[], pairs: Record<string, number> }}
 *   - `triggered`: alerts that fired in this call
 *   - `pairs`:     latest fill_price per pair from the trades array
 *
 * @example
 * // In your dashboard refresh loop:
 * import { checkAlerts } from './components/price-alerts.js';
 *
 * const { trades } = await fetch('/dashboard/trades').then(r => r.json());
 * const { triggered } = checkAlerts(trades);
 * if (triggered.length) console.log('Fired:', triggered.map(a => a.pair));
 */
export function checkAlerts(trades) {
  if (!Array.isArray(trades) || trades.length === 0) {
    return { triggered: [], pairs: {} };
  }

  // ── Step 1: Build latest price per pair from trades ──────────────────────
  /** @type {Record<string, { price: number, timestamp: string }>} */
  const latestByPair = {};

  for (const trade of trades) {
    const pair  = trade.pair;
    const price = Number(trade.fill_price);
    const ts    = trade.timestamp || '';

    if (!pair || isNaN(price) || price <= 0) continue;

    const existing = latestByPair[pair];
    if (!existing || ts > existing.timestamp) {
      latestByPair[pair] = { price, timestamp: ts };
    }
  }

  /** @type {Record<string, number>} */
  const pairPrices = {};
  for (const [pair, { price }] of Object.entries(latestByPair)) {
    pairPrices[pair] = price;
  }

  // ── Step 2: Evaluate each active alert ──────────────────────────────────
  const triggered = [];

  for (const alert of _state.alerts) {
    if (alert.triggered) continue;

    const currentPrice = pairPrices[alert.pair];
    if (currentPrice == null) continue; // No data for this pair in trades

    if (_shouldTrigger(alert, currentPrice)) {
      const ts = latestByPair[alert.pair]?.timestamp ?? new Date().toISOString();
      _markTriggered(alert.id, ts);
      triggered.push({ ...alert, triggeredAt: ts });

      // Visual + audio notification
      _showBanner(alert, currentPrice);
      if (_state.soundEnabled) _playBeep();
    }
  }

  // ── Step 3: Update lastPrices for cross-detection next cycle ─────────────
  Object.assign(_state.lastPrices, pairPrices);

  // ── Step 4: Re-render all mounted widgets if anything changed ────────────
  if (triggered.length > 0) {
    for (const containerId of _state.mountedContainers) {
      const container = document.getElementById(containerId);
      if (container) _renderWidget(container);
    }
  }

  return { triggered, pairs: pairPrices };
}
