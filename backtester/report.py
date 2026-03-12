"""
report.py — Standalone HTML report generator for backtest results.

Generates a mobile-friendly HTML file with:
  * Summary metrics table
  * Equity curve (SVG)
  * Drawdown chart (SVG)
  * Trade log table (sortable via JS)
  * Monthly returns heatmap
  * Parameter optimisation results (optional)

Usage
-----
    from backtester.report import generate_report
    generate_report(result, output_path="data/reports/btcusdt_4h.html")
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backtester.models import BacktestResult, Trade


REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

def generate_report(
    result: BacktestResult,
    output_path: Optional[str] = None,
    title: str = "Backtest Report",
    opt_results: Optional[List] = None,
) -> Path:
    """
    Generate a standalone HTML report.

    Parameters
    ----------
    result : BacktestResult
        Output from BacktestEngine.run().
    output_path : str or None
        Where to save the HTML file.  Defaults to data/reports/<timestamp>.html.
    title : str
        Page/report title.
    opt_results : list or None
        Optimiser results [(params, metrics), ...] to append as a table.

    Returns
    -------
    Path
        Absolute path of the generated HTML file.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = str(REPORT_DIR / f"report_{ts}.html")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    html = _build_html(result, title, opt_results)
    out.write_text(html, encoding="utf-8")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTML builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_html(
    result: BacktestResult,
    title: str,
    opt_results: Optional[List],
) -> str:
    m  = result.metrics
    eq = result.equity_curve
    dd = result.drawdown

    equity_svg   = _equity_svg(eq)
    drawdown_svg = _drawdown_svg(dd)
    monthly_html = _monthly_heatmap(eq)
    trades_html  = _trades_table(result.trades)
    params_html  = _params_table(result.params)
    metrics_html = _metrics_table(m)
    opt_html     = _opt_table(opt_results) if opt_results else ""

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{_esc(title)}</title>
<style>
  :root {{
    --bg: #0f0f13; --card: #1a1a22; --border: #2e2e3e;
    --text: #e0e0e8; --muted: #6e6e88; --green: #22c55e;
    --red: #ef4444; --blue: #60a5fa; --orange: #f97316;
    --yellow: #eab308;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px; line-height: 1.5; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; color: var(--blue); }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }}
  .card .label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .card .value {{ font-size: 1.2rem; font-weight: 700; margin-top: 4px; }}
  .pos {{ color: var(--green); }} .neg {{ color: var(--red); }} .neu {{ color: var(--text); }}
  .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 20px; margin-bottom: 24px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
    color: var(--muted); font-weight: 600; cursor: pointer; user-select: none; }}
  th:hover {{ color: var(--blue); }}
  td {{ padding: 7px 10px; border-bottom: 1px solid rgba(46,46,62,0.5); }}
  tr:hover td {{ background: rgba(96,165,250,0.04); }}
  .svg-wrap {{ width: 100%; overflow: hidden; }}
  svg {{ width: 100%; height: auto; display: block; }}
  .heatmap-grid {{ display: grid; grid-template-columns: 60px repeat(12, 1fr); gap: 3px; font-size: 0.7rem; }}
  .hm-cell {{ padding: 4px 2px; text-align: center; border-radius: 3px; color: var(--text); }}
  .hm-header {{ color: var(--muted); font-weight: 600; text-align: center; padding: 4px 2px; }}
  .hm-label {{ color: var(--muted); display: flex; align-items: center; }}
  @media(max-width:600px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
</style>
</head>
<body>
<div class="container">
  <h1>{_esc(title)}</h1>
  <div class="subtitle">Generated {generated_at}</div>

  <!-- KPI Cards -->
  {_kpi_cards(m)}

  <!-- Equity Curve -->
  <div class="section">
    <h2>Equity Curve</h2>
    <div class="svg-wrap">{equity_svg}</div>
  </div>

  <!-- Drawdown -->
  <div class="section">
    <h2>Drawdown</h2>
    <div class="svg-wrap">{drawdown_svg}</div>
  </div>

  <!-- Monthly Returns -->
  <div class="section">
    <h2>Monthly Returns</h2>
    {monthly_html}
  </div>

  <!-- Metrics -->
  <div class="section">
    <h2>All Metrics</h2>
    {metrics_html}
  </div>

  <!-- Parameters -->
  <div class="section">
    <h2>Strategy Parameters</h2>
    {params_html}
  </div>

  <!-- Trade Log -->
  <div class="section">
    <h2>Trade Log ({len(result.trades)} trades)</h2>
    {trades_html}
  </div>

  {'<div class="section"><h2>Optimisation Results</h2>' + opt_html + '</div>' if opt_html else ''}
</div>
{_sort_script()}
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# KPI cards
# ──────────────────────────────────────────────────────────────────────────────

def _kpi_cards(m: Dict[str, Any]) -> str:
    def card(label: str, value: str, cls: str = "neu") -> str:
        return f'<div class="card"><div class="label">{_esc(label)}</div><div class="value {cls}">{_esc(value)}</div></div>'

    def pct_cls(v: float) -> str:
        return "pos" if v > 0 else ("neg" if v < 0 else "neu")

    net_pct = float(m.get("net_profit_pct", 0))
    dd_pct  = float(m.get("max_drawdown_pct", 0))
    cards = [
        card("Net Profit", f"{m.get('net_profit_usd', 0):+,.2f} ({net_pct:.2f}%)", pct_cls(net_pct)),
        card("CAGR", f"{m.get('cagr_pct', 0):.2f}%", pct_cls(float(m.get('cagr_pct', 0)))),
        card("Sharpe", f"{m.get('sharpe_ratio', 0):.3f}", "pos" if float(m.get('sharpe_ratio',0))>1 else "neu"),
        card("Max DD", f"{dd_pct:.2f}%", "neg" if dd_pct < 0 else "neu"),
        card("Win Rate", f"{m.get('win_rate_pct', 0):.1f}%", "neu"),
        card("Profit Factor", f"{m.get('profit_factor', 0):.3f}", "pos" if float(m.get('profit_factor',0))>1 else "neg"),
        card("Trades", str(m.get("total_trades", 0)), "neu"),
        card("Sortino", f"{m.get('sortino_ratio', 0):.3f}", "neu"),
    ]
    return '<div class="grid">' + "".join(cards) + "</div>"


# ──────────────────────────────────────────────────────────────────────────────
# SVG charts
# ──────────────────────────────────────────────────────────────────────────────

def _equity_svg(equity: pd.Series, width: int = 800, height: int = 220) -> str:
    return _line_svg(equity, width, height, color="#60a5fa", fill_color="rgba(96,165,250,0.1)")


def _drawdown_svg(drawdown: pd.Series, width: int = 800, height: int = 120) -> str:
    return _line_svg(drawdown, width, height, color="#ef4444", fill_color="rgba(239,68,68,0.2)", invert=True)


def _line_svg(
    series: pd.Series,
    width: int,
    height: int,
    color: str,
    fill_color: str,
    invert: bool = False,
) -> str:
    if series.empty:
        return f'<svg viewBox="0 0 {width} {height}"><text x="10" y="20" fill="#888">No data</text></svg>'

    values = series.values.astype(float)
    n = len(values)
    pad_l, pad_r, pad_t, pad_b = 50, 10, 10, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    v_min = float(values.min())
    v_max = float(values.max())
    v_range = v_max - v_min if v_max != v_min else 1.0

    def tx(i: int) -> float:
        return pad_l + (i / (n - 1)) * plot_w if n > 1 else pad_l

    def ty(v: float) -> float:
        norm = (v - v_min) / v_range
        if invert:
            norm = 1 - norm
        return pad_t + (1 - norm) * plot_h

    # Build polyline points
    pts = " ".join(f"{tx(i):.1f},{ty(v):.1f}" for i, v in enumerate(values))

    # Filled area path
    x0 = tx(0)
    x_last = tx(n - 1)
    base_y = ty(0.0) if not invert else ty(v_max)
    fill_path = (
        f"M {x0} {base_y} "
        + " ".join(f"L {tx(i):.1f} {ty(v):.1f}" for i, v in enumerate(values))
        + f" L {x_last} {base_y} Z"
    )

    # Y-axis labels (3 ticks)
    y_ticks = ""
    for i in range(3):
        frac = i / 2
        tick_v = v_min + frac * v_range
        tick_y = ty(tick_v)
        label = f"${tick_v:,.0f}" if abs(tick_v) >= 1 else f"{tick_v:.4f}"
        y_ticks += f'<text x="{pad_l - 5}" y="{tick_y + 4}" fill="#6e6e88" font-size="10" text-anchor="end">{label}</text>'
        y_ticks += f'<line x1="{pad_l}" y1="{tick_y}" x2="{pad_l + plot_w}" y2="{tick_y}" stroke="#2e2e3e" stroke-dasharray="3,3"/>'

    # X-axis labels (5 evenly spaced)
    x_ticks = ""
    for i in range(5):
        idx = int(i / 4 * (n - 1))
        tick_x = tx(idx)
        if hasattr(series.index[idx], "strftime"):
            label = series.index[idx].strftime("%Y-%m")
        else:
            label = str(idx)
        x_ticks += f'<text x="{tick_x}" y="{pad_t + plot_h + 20}" fill="#6e6e88" font-size="10" text-anchor="middle">{label}</text>'

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<path d="{fill_path}" fill="{fill_color}"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'{y_ticks}{x_ticks}'
        f'</svg>'
    )


# ──────────────────────────────────────────────────────────────────────────────
# Monthly heatmap
# ──────────────────────────────────────────────────────────────────────────────

def _monthly_heatmap(equity: pd.Series) -> str:
    if equity.empty:
        return "<p>No data</p>"

    monthly = equity.resample("ME").last()
    if monthly.empty:
        return "<p>Not enough data for monthly breakdown</p>"

    returns = monthly.pct_change().dropna() * 100  # percent

    # Organise into year × month
    data: Dict[int, Dict[int, float]] = {}
    for ts, val in returns.items():
        y = ts.year
        mo = ts.month
        data.setdefault(y, {})[mo] = float(val)

    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    def cell_color(v: float) -> str:
        if v > 10:  return "#16a34a"
        if v > 5:   return "#22c55e"
        if v > 2:   return "#4ade80"
        if v > 0:   return "#86efac"
        if v > -2:  return "#fca5a5"
        if v > -5:  return "#f87171"
        if v > -10: return "#ef4444"
        return "#b91c1c"

    header_row = '<div class="hm-label"></div>' + "".join(
        f'<div class="hm-header">{m}</div>' for m in MONTHS
    )

    rows_html = header_row
    for year in sorted(data.keys()):
        rows_html += f'<div class="hm-label">{year}</div>'
        for mo in range(1, 13):
            v = data[year].get(mo)
            if v is None:
                rows_html += '<div class="hm-cell" style="background:#1a1a22;color:#444">—</div>'
            else:
                bg = cell_color(v)
                rows_html += f'<div class="hm-cell" style="background:{bg}">{v:+.1f}%</div>'

    return f'<div class="heatmap-grid">{rows_html}</div>'


# ──────────────────────────────────────────────────────────────────────────────
# Tables
# ──────────────────────────────────────────────────────────────────────────────

def _metrics_table(m: Dict[str, Any]) -> str:
    skip = {"exit_reasons", "periods_per_year"}
    rows = ""
    for k, v in m.items():
        if k in skip:
            continue
        if isinstance(v, float):
            display = f"{v:,.4f}"
        elif isinstance(v, int):
            display = f"{v:,}"
        else:
            display = str(v)
        rows += f"<tr><td>{_esc(k)}</td><td>{_esc(display)}</td></tr>"
    return f'<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>{rows}</tbody></table>'


def _params_table(params: Dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(str(v))}</td></tr>"
        for k, v in params.items()
    )
    return f'<table><thead><tr><th>Parameter</th><th>Value</th></tr></thead><tbody>{rows}</tbody></table>'


def _trades_table(trades: List[Trade]) -> str:
    if not trades:
        return "<p>No trades.</p>"

    header = (
        "<tr>"
        "<th onclick=\"sortTable(this)\">ID</th>"
        "<th onclick=\"sortTable(this)\">Dir</th>"
        "<th onclick=\"sortTable(this)\">Entry Time</th>"
        "<th onclick=\"sortTable(this)\">Exit Time</th>"
        "<th onclick=\"sortTable(this)\">Entry $</th>"
        "<th onclick=\"sortTable(this)\">Exit $</th>"
        "<th onclick=\"sortTable(this)\">PnL $</th>"
        "<th onclick=\"sortTable(this)\">PnL %</th>"
        "<th onclick=\"sortTable(this)\">Exit Reason</th>"
        "</tr>"
    )
    rows = ""
    for t in trades:
        cls = "pos" if t.pnl > 0 else "neg"
        rows += (
            f"<tr>"
            f"<td>{t.trade_id}</td>"
            f"<td>{t.direction}</td>"
            f"<td>{str(t.entry_time)[:16]}</td>"
            f"<td>{str(t.exit_time)[:16]}</td>"
            f"<td>{t.entry_price:,.4f}</td>"
            f"<td>{t.exit_price:,.4f}</td>"
            f"<td class='{cls}'>{t.pnl:+,.2f}</td>"
            f"<td class='{cls}'>{t.pnl_pct:+.2f}%</td>"
            f"<td>{t.exit_reason}</td>"
            f"</tr>"
        )
    return f'<table id="trades-table"><thead>{header}</thead><tbody>{rows}</tbody></table>'


def _opt_table(opt_results: List) -> str:
    if not opt_results:
        return ""
    rows = ""
    for rank, (params, metrics) in enumerate(opt_results[:50], 1):
        pf  = metrics.get("profit_factor", 0)
        sh  = metrics.get("sharpe_ratio", 0)
        ret = metrics.get("net_profit_pct", 0)
        mdd = metrics.get("max_drawdown_pct", 0)
        p_str = ", ".join(f"{k}={v}" for k, v in params.items())
        cls = "pos" if float(ret) > 0 else "neg"
        rows += (
            f"<tr>"
            f"<td>{rank}</td>"
            f"<td style='font-size:0.75rem'>{_esc(p_str)}</td>"
            f"<td class='{cls}'>{float(ret):+.2f}%</td>"
            f"<td>{float(pf):.3f}</td>"
            f"<td>{float(sh):.3f}</td>"
            f"<td class='neg'>{float(mdd):.2f}%</td>"
            f"</tr>"
        )
    header = "<tr><th>#</th><th>Params</th><th>Return%</th><th>PF</th><th>Sharpe</th><th>MaxDD%</th></tr>"
    return f'<table><thead>{header}</thead><tbody>{rows}</tbody></table>'


def _sort_script() -> str:
    return """<script>
function sortTable(th) {
  const table = th.closest('table');
  const tbody = table.querySelector('tbody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  const idx   = Array.from(th.parentNode.children).indexOf(th);
  const asc   = th.dataset.sort !== 'asc';
  th.dataset.sort = asc ? 'asc' : 'desc';
  rows.sort((a, b) => {
    const av = a.children[idx].textContent.replace(/[,$+%]/g,'').trim();
    const bv = b.children[idx].textContent.replace(/[,$+%]/g,'').trim();
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  rows.forEach(r => tbody.appendChild(r));
}
</script>"""


# ──────────────────────────────────────────────────────────────────────────────
# Util
# ──────────────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Minimal HTML escaping."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
