/**
 * @fileoverview Strategy Comparison Widget — Lighthouse Trading Dashboard
 *
 * A self-contained ES module that renders a side-by-side strategy comparison
 * widget with a radar chart, color-coded metrics table, risk-adjusted scores,
 * and an overall weighted recommendation engine.
 *
 * @module strategy-compare
 *
 * Public API:
 *   renderStrategyCompare(containerId)  — Mount the comparison widget into a DOM element
 *   updateStrategies()                  — Re-fetch strategies from API and refresh UI
 *
 * Dependencies (expected on window / CDN):
 *   Chart.js 4.x  — https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js
 *
 * @version 1.0.0
 */

'use strict';

/* ═══════════════════════════════════════════════════════════════════════════ */
/* CONSTANTS                                                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

/** API endpoint to fetch all strategies. */
const STRATEGIES_ENDPOINT = '/strategies';

/** Maximum strategies a user may compare simultaneously. */
const MAX_COMPARE = 3;

/** Minimum strategies required before comparison renders. */
const MIN_COMPARE = 2;

/** localStorage key to persist the user's last strategy selection. */
const STORAGE_KEY = 'lh_compare_selection';

/**
 * Scoring weights for the recommendation engine.
 * Must sum to 1.0.
 * @type {{ return: number, mdd: number, pf: number, wr: number, trades: number }}
 */
const SCORE_WEIGHTS = {
  return: 0.30,
  mdd:    0.25,
  pf:     0.20,
  wr:     0.15,
  trades: 0.10,
};

/**
 * Radar chart axis labels (display order).
 * These map to normalised metrics computed by _normalizeMetrics().
 */
const RADAR_AXES = ['Return', 'Risk (MDD⁻¹)', 'Profit Factor', 'Win Rate', 'Trade Freq'];

/**
 * Colour palette for up to 3 compared strategies.
 * Matches the dashboard's accent colours.
 */
const STRATEGY_COLORS = [
  { stroke: '#3b82f6', fill: 'rgba(59,130,246,.18)' },  // blue
  { stroke: '#8b5cf6', fill: 'rgba(139,92,246,.18)' },  // purple
  { stroke: '#22c55e', fill: 'rgba(34,197,94,.18)' },   // green
];

/* ═══════════════════════════════════════════════════════════════════════════ */
/* MODULE STATE                                                               */
/* ═══════════════════════════════════════════════════════════════════════════ */

/** @type {Array<Object>} All strategies fetched from the API. */
let _allStrategies = [];

/** @type {Set<string>} IDs of currently selected strategies. */
let _selected = new Set();

/** @type {Chart|null} Active Chart.js radar instance. */
let _radarChart = null;

/** @type {string|null} The DOM container id passed to renderStrategyCompare(). */
let _containerId = null;

/* ═══════════════════════════════════════════════════════════════════════════ */
/* API                                                                        */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Fetch all strategies from the backend API.
 *
 * @returns {Promise<Array<Object>>} Array of strategy objects.
 */
async function _fetchStrategies() {
  const res = await fetch(STRATEGIES_ENDPOINT, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Strategies API error: ${res.status} ${res.statusText}`);
  const data = await res.json();
  return Array.isArray(data.strategies) ? data.strategies : [];
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* METRICS HELPERS                                                            */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Compute the Return/MDD risk-adjusted ratio for a strategy.
 *
 * @param {Object} s  Strategy object.
 * @returns {number|null}
 */
function _returnMddRatio(s) {
  if (s.tv_return == null || s.tv_mdd == null || s.tv_mdd === 0) return null;
  return +(s.tv_return / s.tv_mdd).toFixed(2);
}

/**
 * Estimate a simplified Sharpe-like ratio.
 * Uses TV return as annualised proxy and MDD as risk proxy (no std dev available).
 * Formula: (Return% / MDD%) * sqrt(trades) / 10  — rough ordinal proxy.
 * Returns null when insufficient data.
 *
 * @param {Object} s  Strategy object.
 * @returns {number|null}
 */
function _sharpeEstimate(s) {
  if (s.tv_return == null || s.tv_mdd == null || s.tv_mdd === 0) return null;
  const trades = s.tv_trades ?? 30; // fallback assumption
  const ratio = s.tv_return / s.tv_mdd;
  const sharpe = (ratio * Math.sqrt(trades)) / 100;
  return +sharpe.toFixed(2);
}

/**
 * Estimate trades per year for a strategy.
 * When tv_trades is null a reasonable fallback of 30 is used.
 *
 * @param {Object} s  Strategy object.
 * @returns {number}
 */
function _tradesPerYear(s) {
  // Strategy lab data is typically multi-year backtests — assume ~3 year average
  return s.tv_trades != null ? +(s.tv_trades / 3).toFixed(1) : null;
}

/**
 * Normalize a set of values to [0, 100] range for radar chart.
 * Handles inverses (where lower raw value = better score).
 *
 * @param {number[]} values   Raw values (may include null — treated as 0).
 * @param {boolean}  inverse  If true, lower raw = higher normalised score.
 * @returns {number[]}        Normalised scores [0-100].
 */
function _normalize(values, inverse = false) {
  const clean = values.map(v => v ?? 0);
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  if (max === min) return clean.map(() => 50); // all equal → mid-point
  return clean.map(v => {
    const norm = ((v - min) / (max - min)) * 100;
    return inverse ? +(100 - norm).toFixed(1) : +norm.toFixed(1);
  });
}

/**
 * Build normalised radar datasets for a list of strategies.
 *
 * Axes order matches RADAR_AXES:
 *  [0] Return        — higher is better
 *  [1] Risk (MDD⁻¹)  — lower MDD is better (inverted)
 *  [2] Profit Factor — higher is better
 *  [3] Win Rate      — higher is better
 *  [4] Trade Freq    — higher is better (more data → more reliable)
 *
 * @param {Object[]} strategies  Array of strategy objects to compare.
 * @returns {number[][]}         Parallel array of [r,m,pf,wr,tf] per strategy.
 */
function _normalizeMetrics(strategies) {
  const returns  = strategies.map(s => s.tv_return   ?? 0);
  const mdds     = strategies.map(s => s.tv_mdd      ?? 0);
  const pfs      = strategies.map(s => s.tv_pf       ?? 0);
  const wrs      = strategies.map(s => s.tv_wr       ?? 0);
  const tradesYr = strategies.map(s => _tradesPerYear(s) ?? 0);

  const normReturn = _normalize(returns,  false);
  const normMdd    = _normalize(mdds,     true);   // inverse
  const normPf     = _normalize(pfs,      false);
  const normWr     = _normalize(wrs,      false);
  const normTrades = _normalize(tradesYr, false);

  return strategies.map((_, i) => [
    normReturn[i],
    normMdd[i],
    normPf[i],
    normWr[i],
    normTrades[i],
  ]);
}

/**
 * Compute a single overall score (0-100) for a strategy using SCORE_WEIGHTS.
 * Requires normalised radar values for the full comparison set.
 *
 * @param {number[]} normValues  [Return, MDD⁻¹, PF, WR, Trades] normalised 0-100.
 * @returns {number}             Weighted score 0-100.
 */
function _overallScore(normValues) {
  const [r, m, pf, wr, t] = normValues;
  const score =
    r  * SCORE_WEIGHTS.return  +
    m  * SCORE_WEIGHTS.mdd     +
    pf * SCORE_WEIGHTS.pf      +
    wr * SCORE_WEIGHTS.wr      +
    t  * SCORE_WEIGHTS.trades;
  return +score.toFixed(1);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* RENDERING HELPERS                                                          */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Format a value for display in the comparison table.
 *
 * @param {*}      value   Raw value.
 * @param {string} type    One of: 'pct', 'x', 'count', 'ratio', 'raw'.
 * @returns {string}
 */
function _fmt(value, type = 'raw') {
  if (value == null) return '<span class="sc-na">N/A</span>';
  switch (type) {
    case 'pct':   return `${Number(value).toLocaleString()}%`;
    case 'x':     return `${Number(value).toFixed(2)}x`;
    case 'count': return Number(value).toLocaleString();
    case 'ratio': return Number(value).toFixed(2);
    default:      return String(value);
  }
}

/**
 * Determine cell highlight class based on whether this strategy has the best
 * or worst value for a metric.
 *
 * @param {number}   idx       Index of the strategy in the compared array.
 * @param {number[]} values    Raw values for all compared strategies.
 * @param {boolean}  lowerBetter  If true, the lowest value is "best".
 * @returns {string}  CSS class: 'sc-best' | 'sc-worst' | ''
 */
function _highlightClass(idx, values, lowerBetter = false) {
  const clean = values.map(v => v ?? (lowerBetter ? Infinity : -Infinity));
  const best  = lowerBetter ? Math.min(...clean) : Math.max(...clean);
  const worst = lowerBetter ? Math.max(...clean) : Math.min(...clean);
  const v = clean[idx];
  if (values[idx] == null) return '';
  if (v === best  && best  !== worst) return 'sc-best';
  if (v === worst && best  !== worst) return 'sc-worst';
  return '';
}

/**
 * Build the inner HTML for the strategy selector section.
 *
 * @param {Object[]} strategies  All available strategies.
 * @param {Set<string>} selected  Currently selected IDs.
 * @returns {string} HTML string.
 */
function _buildSelectorHTML(strategies, selected) {
  const chips = strategies.map(s => {
    const isSelected  = selected.has(s.id);
    const isDisabled  = !isSelected && selected.size >= MAX_COMPARE;
    const statusClass = s.status === 'champion' ? 'sc-chip-champion'
                      : s.status === 'candidate' ? 'sc-chip-candidate'
                      : 'sc-chip-retired';
    const checkedAttr = isSelected  ? 'checked' : '';
    const disabledAttr = isDisabled ? 'disabled' : '';
    return `
      <label class="sc-chip ${statusClass} ${isSelected ? 'selected' : ''} ${isDisabled ? 'disabled' : ''}"
             data-id="${s.id}">
        <input type="checkbox" ${checkedAttr} ${disabledAttr}
               data-id="${s.id}"
               onchange="window._scToggle('${s.id}', this.checked)" />
        <span class="sc-chip-coin">${s.coin}</span>
        <span class="sc-chip-name">${s.name}</span>
        ${s.status === 'champion' ? '<span class="sc-chip-badge">🏆</span>' : ''}
      </label>
    `;
  }).join('');

  return `
    <div class="sc-selector-wrap">
      <p class="sc-selector-hint">
        Select <strong>2–3 strategies</strong> to compare
        <span class="sc-count-badge">${selected.size}/${MAX_COMPARE}</span>
      </p>
      <div class="sc-chips">${chips}</div>
    </div>
  `;
}

/**
 * Build the comparison table HTML for the currently selected strategies.
 *
 * @param {Object[]} strategies  Selected strategy objects.
 * @returns {string} HTML string.
 */
function _buildTableHTML(strategies) {
  // Pre-compute arrays for highlight logic
  const returns  = strategies.map(s => s.tv_return);
  const mdds     = strategies.map(s => s.tv_mdd);
  const pfs      = strategies.map(s => s.tv_pf);
  const wrs      = strategies.map(s => s.tv_wr);
  const trades   = strategies.map(s => s.tv_trades);
  const ratios   = strategies.map(s => _returnMddRatio(s));
  const sharpes  = strategies.map(s => _sharpeEstimate(s));

  // Header row
  const headerCells = strategies.map((s, i) => {
    const color = STRATEGY_COLORS[i % STRATEGY_COLORS.length];
    return `<th style="border-top:3px solid ${color.stroke}">
              <div class="sc-th-name">${s.name}</div>
              <div class="sc-th-sub">${s.pair} · ${s.timeframe}</div>
            </th>`;
  }).join('');

  /**
   * Render a data row for the table.
   * @param {string}   label       Row label.
   * @param {Array}    vals        Raw values.
   * @param {string}   type        Format type passed to _fmt().
   * @param {boolean}  lowerBetter Highlight direction.
   * @param {string}   [rowClass]  Optional extra row class.
   * @returns {string}
   */
  const row = (label, vals, type, lowerBetter = false, rowClass = '') => {
    const cells = vals.map((v, i) => {
      const cls = _highlightClass(i, vals, lowerBetter);
      return `<td class="${cls}">${_fmt(v, type)}</td>`;
    }).join('');
    return `<tr class="${rowClass}"><td class="sc-row-label">${label}</td>${cells}</tr>`;
  };

  return `
    <div class="sc-table-wrap">
      <table class="sc-table">
        <thead>
          <tr>
            <th class="sc-metric-col">Metric</th>
            ${headerCells}
          </tr>
        </thead>
        <tbody>
          ${row('TV Return',        returns, 'pct',   false)}
          ${row('Max Drawdown',     mdds,    'pct',   true)}
          ${row('Profit Factor',    pfs,     'x',     false)}
          ${row('Win Rate',         wrs,     'pct',   false)}
          ${row('Trade Count',      trades,  'count', false)}
          <tr class="sc-divider"><td colspan="${strategies.length + 1}">Risk-Adjusted</td></tr>
          ${row('Return / MDD',     ratios,  'ratio', false)}
          ${row('Sharpe (est.)',    sharpes, 'ratio', false, 'sc-row-muted')}
        </tbody>
      </table>
    </div>
  `;
}

/**
 * Build the scores section HTML (recommendation engine output).
 *
 * @param {Object[]} strategies   Selected strategy objects.
 * @param {number[][]} normMatrix  Normalised metrics per strategy.
 * @returns {string} HTML string.
 */
function _buildScoresHTML(strategies, normMatrix) {
  const scores = normMatrix.map(_overallScore);
  const maxScore = Math.max(...scores);

  const cards = strategies.map((s, i) => {
    const score = scores[i];
    const color = STRATEGY_COLORS[i % STRATEGY_COLORS.length];
    const isWinner = score === maxScore && scores.filter(x => x === maxScore).length === 1;
    const barWidth = Math.max(4, score);
    const scoreClass = score >= 70 ? 'sc-score-high' : score >= 40 ? 'sc-score-mid' : 'sc-score-low';

    return `
      <div class="sc-score-card ${isWinner ? 'sc-score-winner' : ''}">
        ${isWinner ? '<div class="sc-winner-badge">⭐ Recommended</div>' : ''}
        <div class="sc-score-name" style="color:${color.stroke}">${s.name}</div>
        <div class="sc-score-val ${scoreClass}">${score}</div>
        <div class="sc-score-label">/ 100</div>
        <div class="sc-score-bar-track">
          <div class="sc-score-bar-fill" style="width:${barWidth}%; background:${color.stroke}"></div>
        </div>
        <div class="sc-score-breakdown">
          <span title="Return (30%)">R: ${normMatrix[i][0].toFixed(0)}</span>
          <span title="MDD⁻¹ (25%)">D: ${normMatrix[i][1].toFixed(0)}</span>
          <span title="Profit Factor (20%)">PF: ${normMatrix[i][2].toFixed(0)}</span>
          <span title="Win Rate (15%)">WR: ${normMatrix[i][3].toFixed(0)}</span>
          <span title="Trades (10%)">T: ${normMatrix[i][4].toFixed(0)}</span>
        </div>
      </div>
    `;
  }).join('');

  return `
    <div class="sc-scores-section">
      <div class="sc-section-title">📊 Strategy Recommendation</div>
      <p class="sc-scores-hint">Weighted score: Return 30% · MDD 25% · PF 20% · Win Rate 15% · Trades 10%</p>
      <div class="sc-score-cards">${cards}</div>
    </div>
  `;
}

/**
 * Build or update the radar chart using Chart.js.
 *
 * @param {string}    canvasId    ID of the <canvas> element.
 * @param {Object[]}  strategies  Selected strategy objects.
 * @param {number[][]} normMatrix  Normalised radar values.
 */
function _renderRadar(canvasId, strategies, normMatrix) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  // Destroy previous instance to avoid double-render
  if (_radarChart) {
    _radarChart.destroy();
    _radarChart = null;
  }

  const datasets = strategies.map((s, i) => {
    const color = STRATEGY_COLORS[i % STRATEGY_COLORS.length];
    return {
      label: s.name,
      data: normMatrix[i],
      borderColor: color.stroke,
      backgroundColor: color.fill,
      borderWidth: 2,
      pointBackgroundColor: color.stroke,
      pointRadius: 4,
      pointHoverRadius: 6,
    };
  });

  _radarChart = new window.Chart(canvas, {
    type: 'radar',
    data: {
      labels: RADAR_AXES,
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      animation: { duration: 400 },
      scales: {
        r: {
          min: 0,
          max: 100,
          ticks: {
            stepSize: 25,
            color: '#64748b',
            backdropColor: 'transparent',
            font: { size: 10 },
          },
          grid: { color: 'rgba(15,23,42,.1)' },
          angleLines: { color: 'rgba(15,23,42,.1)' },
          pointLabels: {
            color: '#475569',
            font: { size: 11, weight: '600' },
          },
        },
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: '#475569',
            font: { size: 12 },
            padding: 16,
            usePointStyle: true,
            pointStyleWidth: 10,
          },
        },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${ctx.raw.toFixed(0)}/100`,
          },
        },
      },
    },
  });
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* CSS INJECTION                                                              */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Inject component-scoped CSS into the document <head> (once).
 */
function _injectStyles() {
  const STYLE_ID = 'sc-styles';
  if (document.getElementById(STYLE_ID)) return;

  const css = `
    /* ── Strategy Compare Widget ─────────────────────────────────────────── */
    .sc-wrap {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px;
      color: #0f172a;
    }

    /* Selector */
    .sc-selector-wrap { margin-bottom: 20px; }
    .sc-selector-hint {
      font-size: .8rem; color: #64748b; margin-bottom: 10px;
      display: flex; align-items: center; gap: 8px;
    }
    .sc-count-badge {
      display: inline-block; padding: 2px 8px; border-radius: 20px;
      background: rgba(59,130,246,.12); color: #3b82f6;
      font-size: .72rem; font-weight: 700;
    }
    .sc-chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .sc-chip {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 6px 12px; border-radius: 20px; cursor: pointer;
      border: 1.5px solid rgba(15,23,42,.12);
      background: #fff; transition: all .2s;
      user-select: none;
    }
    .sc-chip input[type=checkbox] { display: none; }
    .sc-chip:hover:not(.disabled) { border-color: #3b82f6; background: rgba(59,130,246,.06); }
    .sc-chip.selected { border-color: #3b82f6; background: rgba(59,130,246,.1); }
    .sc-chip.disabled { opacity: .45; cursor: not-allowed; }
    .sc-chip-champion.selected  { border-color: #22c55e; background: rgba(34,197,94,.1); }
    .sc-chip-candidate.selected { border-color: #eab308; background: rgba(234,179,8,.1); }
    .sc-chip-retired.selected   { border-color: #64748b; background: rgba(100,116,139,.1); }
    .sc-chip-coin {
      font-size: .65rem; font-weight: 700; text-transform: uppercase;
      padding: 1px 6px; border-radius: 4px;
      background: rgba(15,23,42,.07); color: #475569; letter-spacing: .4px;
    }
    .sc-chip-name { font-size: .82rem; font-weight: 600; }
    .sc-chip-badge { font-size: .8rem; }

    /* Layout */
    .sc-layout {
      display: grid;
      grid-template-columns: 1fr 340px;
      gap: 20px;
      align-items: start;
    }
    @media (max-width: 900px) {
      .sc-layout { grid-template-columns: 1fr; }
    }

    /* Table */
    .sc-table-wrap { overflow-x: auto; }
    .sc-table {
      width: 100%; border-collapse: collapse;
      font-size: .82rem; font-variant-numeric: tabular-nums;
    }
    .sc-table thead th {
      padding: 10px 14px; background: #f8fafc;
      text-align: center; font-weight: 700;
      border-bottom: 1px solid rgba(15,23,42,.1);
    }
    .sc-table .sc-metric-col { text-align: left; }
    .sc-th-name { font-size: .85rem; font-weight: 700; color: #0f172a; }
    .sc-th-sub  { font-size: .68rem; color: #64748b; font-weight: 500; margin-top: 2px; }

    .sc-table tbody td {
      padding: 8px 14px; text-align: center;
      border-bottom: 1px solid rgba(15,23,42,.06);
      font-weight: 600; transition: background .15s;
    }
    .sc-table .sc-row-label {
      text-align: left; font-weight: 500; color: #475569; white-space: nowrap;
    }
    .sc-table .sc-divider td {
      background: #f1f5f9; font-size: .7rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: .6px; color: #64748b;
      padding: 5px 14px;
    }
    .sc-table .sc-row-muted td { color: #94a3b8; }

    /* Highlight classes */
    .sc-best  { background: rgba(34,197,94,.14)  !important; color: #16a34a !important; }
    .sc-worst { background: rgba(239,68,68,.1)   !important; color: #dc2626 !important; }
    .sc-na    { color: #94a3b8; font-weight: 400; font-style: italic; }

    /* Radar chart */
    .sc-radar-wrap {
      background: #fff; border-radius: 10px;
      border: 1px solid rgba(15,23,42,.08);
      padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
    }
    .sc-radar-title {
      font-size: .78rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: .6px; color: #64748b; margin-bottom: 12px; text-align: center;
    }
    .sc-radar-canvas { max-height: 300px; }

    /* Scores */
    .sc-scores-section { margin-top: 20px; }
    .sc-section-title {
      font-size: .88rem; font-weight: 700; color: #0f172a; margin-bottom: 4px;
    }
    .sc-scores-hint {
      font-size: .72rem; color: #64748b; margin-bottom: 14px;
    }
    .sc-score-cards {
      display: flex; flex-wrap: wrap; gap: 12px;
    }
    .sc-score-card {
      flex: 1; min-width: 140px;
      background: #fff; border-radius: 10px;
      border: 1.5px solid rgba(15,23,42,.1);
      padding: 14px 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,.05);
      position: relative; text-align: center;
    }
    .sc-score-card.sc-score-winner {
      border-color: #3b82f6;
      box-shadow: 0 0 0 3px rgba(59,130,246,.15);
    }
    .sc-winner-badge {
      position: absolute; top: -10px; left: 50%; transform: translateX(-50%);
      background: #3b82f6; color: #fff;
      font-size: .65rem; font-weight: 700;
      padding: 2px 10px; border-radius: 20px; white-space: nowrap;
    }
    .sc-score-name { font-size: .78rem; font-weight: 700; margin-bottom: 6px; }
    .sc-score-val  { font-size: 2rem; font-weight: 800; line-height: 1; }
    .sc-score-label { font-size: .65rem; color: #94a3b8; margin-bottom: 8px; }
    .sc-score-high  { color: #22c55e; }
    .sc-score-mid   { color: #eab308; }
    .sc-score-low   { color: #ef4444; }
    .sc-score-bar-track {
      height: 5px; background: rgba(15,23,42,.08); border-radius: 3px;
      overflow: hidden; margin-bottom: 8px;
    }
    .sc-score-bar-fill { height: 100%; border-radius: 3px; transition: width .5s ease; }
    .sc-score-breakdown {
      display: flex; justify-content: center; gap: 6px;
      font-size: .62rem; color: #94a3b8; font-variant-numeric: tabular-nums;
    }

    /* Empty / loading states */
    .sc-placeholder {
      text-align: center; padding: 48px 24px;
      color: #94a3b8; font-size: .88rem;
    }
    .sc-placeholder-icon { font-size: 2.5rem; margin-bottom: 10px; }
    .sc-error { color: #ef4444; }

    .sc-loading {
      display: inline-block; width: 18px; height: 18px;
      border: 2px solid rgba(59,130,246,.2);
      border-top-color: #3b82f6;
      border-radius: 50%;
      animation: sc-spin .7s linear infinite;
      vertical-align: middle; margin-right: 6px;
    }
    @keyframes sc-spin { to { transform: rotate(360deg); } }
  `;

  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = css;
  document.head.appendChild(style);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* MAIN RENDER LOOP                                                           */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Full re-render of the widget into the container.
 * Called whenever strategy selection changes or data refreshes.
 */
function _render() {
  const container = document.getElementById(_containerId);
  if (!container) return;

  const selected = _allStrategies.filter(s => _selected.has(s.id));

  // Header + selector always visible
  const selectorHTML = _buildSelectorHTML(_allStrategies, _selected);

  if (selected.length < MIN_COMPARE) {
    // Not enough selections — show placeholder below selector
    container.innerHTML = `
      <div class="sc-wrap">
        ${selectorHTML}
        <div class="sc-placeholder">
          <div class="sc-placeholder-icon">📈</div>
          <div>Select at least <strong>2 strategies</strong> to compare</div>
        </div>
      </div>
    `;
    return;
  }

  // Compute normalised metrics + scores
  const normMatrix = _normalizeMetrics(selected);
  const canvasId   = `sc-radar-${_containerId}`;

  container.innerHTML = `
    <div class="sc-wrap">
      ${selectorHTML}
      <div class="sc-layout">
        <div>
          ${_buildTableHTML(selected)}
        </div>
        <div class="sc-radar-wrap">
          <div class="sc-radar-title">Radar Comparison</div>
          <canvas id="${canvasId}" class="sc-radar-canvas"></canvas>
        </div>
      </div>
      ${_buildScoresHTML(selected, normMatrix)}
    </div>
  `;

  // Render radar chart after DOM is ready
  requestAnimationFrame(() => _renderRadar(canvasId, selected, normMatrix));
}

/**
 * Persist current selection to localStorage.
 */
function _saveSelection() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([..._selected]));
  } catch (_) { /* storage unavailable — silently ignore */ }
}

/**
 * Restore previously saved selection from localStorage.
 */
function _loadSelection() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const ids = JSON.parse(raw);
      if (Array.isArray(ids)) {
        _selected = new Set(ids.slice(0, MAX_COMPARE));
      }
    }
  } catch (_) { /* corrupt storage — start fresh */ }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* GLOBAL EVENT HANDLER (checkbox toggle)                                    */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Toggle a strategy in/out of the selection.
 * Called via inline onchange on checkboxes (window._scToggle).
 *
 * @param {string}  id       Strategy ID.
 * @param {boolean} checked  Whether the checkbox was just checked.
 */
window._scToggle = function _scToggle(id, checked) {
  if (checked) {
    if (_selected.size < MAX_COMPARE) _selected.add(id);
  } else {
    _selected.delete(id);
  }
  _saveSelection();
  _render();
};

/* ═══════════════════════════════════════════════════════════════════════════ */
/* PUBLIC API                                                                 */
/* ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Mount the Strategy Comparison widget inside a DOM element.
 *
 * Fetches strategies from GET /strategies, restores the last selection from
 * localStorage, then renders the full widget UI. Subsequent calls to
 * updateStrategies() will refresh data without re-mounting.
 *
 * @param {string} containerId  ID of the DOM element to render into.
 * @returns {Promise<void>}
 *
 * @example
 * import { renderStrategyCompare } from './components/strategy-compare.js';
 * await renderStrategyCompare('compare-widget');
 */
export async function renderStrategyCompare(containerId) {
  _containerId = containerId;
  _injectStyles();

  const container = document.getElementById(containerId);
  if (!container) {
    console.warn(`[strategy-compare] Container #${containerId} not found.`);
    return;
  }

  // Show loading state
  container.innerHTML = `
    <div class="sc-wrap">
      <div class="sc-placeholder">
        <span class="sc-loading"></span> Loading strategies…
      </div>
    </div>
  `;

  try {
    _allStrategies = await _fetchStrategies();
    _loadSelection();

    // Auto-select top 2 champions if nothing is saved
    if (_selected.size === 0) {
      const champions = _allStrategies
        .filter(s => s.status === 'champion' && s.tv_return != null)
        .sort((a, b) => (b.tv_return ?? 0) - (a.tv_return ?? 0))
        .slice(0, 2);
      champions.forEach(s => _selected.add(s.id));
    }

    // Prune saved IDs that no longer exist in the API response
    const knownIds = new Set(_allStrategies.map(s => s.id));
    for (const id of [..._selected]) {
      if (!knownIds.has(id)) _selected.delete(id);
    }

    _render();
  } catch (err) {
    container.innerHTML = `
      <div class="sc-wrap">
        <div class="sc-placeholder sc-error">
          <div class="sc-placeholder-icon">⚠️</div>
          <div>Failed to load strategies: ${err.message}</div>
        </div>
      </div>
    `;
    console.error('[strategy-compare] Fetch error:', err);
  }
}

/**
 * Re-fetch strategies from the API and refresh the widget UI.
 *
 * Call this after creating/updating strategies on the server side, or on a
 * polling interval if real-time updates are needed.
 *
 * @returns {Promise<void>}
 *
 * @example
 * // Refresh every 60 seconds
 * setInterval(updateStrategies, 60_000);
 */
export async function updateStrategies() {
  if (!_containerId) {
    console.warn('[strategy-compare] updateStrategies() called before renderStrategyCompare().');
    return;
  }

  try {
    _allStrategies = await _fetchStrategies();

    // Prune any selections that no longer exist
    const knownIds = new Set(_allStrategies.map(s => s.id));
    for (const id of [..._selected]) {
      if (!knownIds.has(id)) _selected.delete(id);
    }

    _render();
  } catch (err) {
    console.error('[strategy-compare] updateStrategies error:', err);
  }
}
