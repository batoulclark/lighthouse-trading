"""
pine_generator.py — Generate PineScript v5 code from a Python strategy.

Currently supports: GaussianChannelStrategy (full implementation)
Other strategies: generates a stub with entry/exit placeholder comments.

Usage
-----
    from backtester.pine_generator import generate_pine
    from backtester.strategies.gaussian_channel import GaussianChannelStrategy

    strategy = GaussianChannelStrategy()
    pine_code = generate_pine(strategy, symbol="BTCUSDT", timeframe="4h")
    Path("gaussian_v7.pine").write_text(pine_code)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from backtester.strategy_base import StrategyBase


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def generate_pine(
    strategy: StrategyBase,
    symbol: str = "BTCUSDT",
    timeframe: str = "4h",
    initial_capital: float = 10_000.0,
    commission_pct: float = 0.04,
    slippage_ticks: int = 1,
    bot_id: str = "your_bot_id",
    output_path: Optional[str] = None,
) -> str:
    """
    Generate PineScript v5 source code for *strategy*.

    Parameters
    ----------
    strategy : StrategyBase
        Instantiated strategy (params read from strategy.params).
    symbol : str
        Used in comments / alert messages.
    timeframe : str
        Used in comments / strategy settings.
    initial_capital : float
        strategy() default_qty_value.
    commission_pct : float
        Commission per side in percent (e.g. 0.04 = 0.04%).
    slippage_ticks : int
        Slippage in ticks for strategy().
    bot_id : str
        Bot ID embedded in the webhook alert JSON.
    output_path : str or None
        If provided, write the file to this path.

    Returns
    -------
    str
        Complete PineScript v5 source code.
    """
    name = strategy.name

    if name == "gaussian_channel":
        code = _pine_gaussian_channel(
            params          = strategy.params,
            symbol          = symbol,
            initial_capital = initial_capital,
            commission_pct  = commission_pct,
            slippage_ticks  = slippage_ticks,
            bot_id          = bot_id,
        )
    elif name == "ma_cross":
        code = _pine_ma_cross(
            params          = strategy.params,
            symbol          = symbol,
            initial_capital = initial_capital,
            commission_pct  = commission_pct,
            slippage_ticks  = slippage_ticks,
            bot_id          = bot_id,
        )
    else:
        code = _pine_stub(strategy, symbol, initial_capital, commission_pct, slippage_ticks, bot_id)

    if output_path:
        from pathlib import Path
        Path(output_path).write_text(code, encoding="utf-8")

    return code


# ──────────────────────────────────────────────────────────────────────────────
# Gaussian Channel PineScript
# ──────────────────────────────────────────────────────────────────────────────

def _pine_gaussian_channel(
    params: Dict[str, Any],
    symbol: str,
    initial_capital: float,
    commission_pct: float,
    slippage_ticks: int,
    bot_id: str,
) -> str:
    p          = params
    period     = int(p.get("period", 100))
    poles      = int(p.get("poles", 3))
    mult       = float(p.get("multiplier", 2.0))
    atr_p      = int(p.get("atr_period", 14))
    use_macd   = bool(p.get("use_macd", False))
    macd_fast  = int(p.get("macd_fast", 12))
    macd_slow  = int(p.get("macd_slow", 26))
    macd_sig   = int(p.get("macd_signal", 9))
    sl_pct     = float(p.get("sl_pct", 0.05)) * 100   # convert to %
    tp_pct     = float(p.get("tp_pct", 0.0))  * 100
    use_tf     = bool(p.get("use_time_filter", False))
    ts_start   = int(p.get("trade_hours_start", 1))
    ts_end     = int(p.get("trade_hours_end", 23))
    use_fund   = bool(p.get("use_funding_filter", False))
    fund_thr   = float(p.get("funding_threshold", 0.001))

    pine_bool = lambda b: "true" if b else "false"

    alert_buy = (
        '{"bot_id": "' + bot_id + '", '
        '"ticker": "{{ticker}}", '
        '"action": "buy", '
        '"order_size": "100%", '
        '"position_size": "1", '
        '"timestamp": "{{timenow}}", '
        '"schema": "2"}'
    )
    alert_sell = (
        '{"bot_id": "' + bot_id + '", '
        '"ticker": "{{ticker}}", '
        '"action": "sell", '
        '"order_size": "100%", '
        '"position_size": "-1", '
        '"timestamp": "{{timenow}}", '
        '"schema": "2"}'
    )
    alert_close = (
        '{"bot_id": "' + bot_id + '", '
        '"ticker": "{{ticker}}", '
        '"action": "close", '
        '"order_size": "100%", '
        '"position_size": "0", '
        '"timestamp": "{{timenow}}", '
        '"schema": "2"}'
    )

    sl_line  = f"strategy.exit('SL/TP Long',  'Long',  stop=strategy.position_avg_price*(1-sl_pct/100), limit=tp_pct>0 ? strategy.position_avg_price*(1+tp_pct/100) : na)" if sl_pct > 0 else ""
    sl_short = f"strategy.exit('SL/TP Short', 'Short', stop=strategy.position_avg_price*(1+sl_pct/100), limit=tp_pct>0 ? strategy.position_avg_price*(1-tp_pct/100) : na)" if sl_pct > 0 else ""

    return f"""//@version=5
// ═══════════════════════════════════════════════════════════════
// Lighthouse Trading — Gaussian Channel Strategy v7
// Auto-generated by backtester/pine_generator.py
// Symbol: {symbol}
// ═══════════════════════════════════════════════════════════════

strategy(
  title             = "Lighthouse GC v7",
  overlay           = true,
  initial_capital   = {int(initial_capital)},
  default_qty_type  = strategy.percent_of_equity,
  default_qty_value = 100,
  commission_type   = strategy.commission.percent,
  commission_value  = {commission_pct},
  slippage          = {slippage_ticks},
  pyramiding        = 0
)

// ── Inputs ─────────────────────────────────────────────────── //
gc_period   = input.int({period},  "GC Period",     minval=10,  maxval=500,  group="Gaussian Channel")
gc_poles    = input.int({poles},   "Poles",         minval=1,   maxval=4,    group="Gaussian Channel")
gc_mult     = input.float({mult},  "Multiplier",    minval=0.1, maxval=10.0, step=0.1, group="Gaussian Channel")
atr_len     = input.int({atr_p},   "ATR Period",    minval=1,   maxval=200,  group="Gaussian Channel")
sl_pct      = input.float({sl_pct:.1f}, "Stop Loss %", minval=0.0, maxval=50.0, step=0.1, group="Risk")
tp_pct      = input.float({tp_pct:.1f}, "Take Profit % (0=off)", minval=0.0, maxval=200.0, step=0.1, group="Risk")
use_macd    = input.bool({pine_bool(use_macd)}, "MACD Filter", group="Filters")
macd_fast   = input.int({macd_fast}, "MACD Fast", group="Filters")
macd_slow   = input.int({macd_slow}, "MACD Slow", group="Filters")
macd_sig_p  = input.int({macd_sig}, "MACD Signal", group="Filters")
use_time    = input.bool({pine_bool(use_tf)}, "Time Filter (UTC)", group="Filters")
time_start  = input.int({ts_start}, "Trade Hour Start", minval=0, maxval=23, group="Filters")
time_end    = input.int({ts_end},   "Trade Hour End",   minval=0, maxval=23, group="Filters")

// ── Gaussian Filter ────────────────────────────────────────── //
f_gaussian(src, length, poles) =>
    float beta  = (1.0 - math.cos(2.0 * math.pi / length)) / (math.pow(2.0, 1.0/poles) - 1.0)
    float alpha = -beta + math.sqrt(beta*beta + 2.0*beta)
    alpha := math.min(alpha, 1.0)
    float f1 = 0.0, f2 = 0.0, f3 = 0.0, f4 = 0.0
    f1 := alpha*src  + (1.0-alpha)*nz(f1[1], src)
    f2 := poles>=2 ? alpha*f1 + (1.0-alpha)*nz(f2[1], src) : f1
    f3 := poles>=3 ? alpha*f2 + (1.0-alpha)*nz(f3[1], src) : f2
    f4 := poles>=4 ? alpha*f3 + (1.0-alpha)*nz(f4[1], src) : f3
    f4

gc_mid  = f_gaussian(close, gc_period, gc_poles)

// ── ATR Band ───────────────────────────────────────────────── //
gc_atr  = ta.atr(atr_len)
upper   = gc_mid + gc_mult * gc_atr
lower   = gc_mid - gc_mult * gc_atr

plot(gc_mid, "GC Mid",   color=color.new(color.blue,   30), linewidth=1)
plot(upper,  "GC Upper", color=color.new(color.green,  20), linewidth=2)
plot(lower,  "GC Lower", color=color.new(color.red,    20), linewidth=2)
fill(plot(upper,"",display=display.none), plot(lower,"",display=display.none),
     color=color.new(color.blue, 92))

// ── MACD Filter ────────────────────────────────────────────── //
[macd_line, signal_line, hist_line] = ta.macd(close, macd_fast, macd_slow, macd_sig_p)
macd_bull = hist_line > 0
macd_bear = hist_line < 0

// ── Time Filter ────────────────────────────────────────────── //
hour_ok = not use_time or (hour(time, "UTC") >= time_start and hour(time, "UTC") < time_end)

// ── Entry / Exit Conditions ────────────────────────────────── //
cross_above_upper = ta.crossover(close, upper)
cross_below_lower = ta.crossunder(close, lower)
cross_back_upper  = ta.crossunder(close, upper)
cross_back_lower  = ta.crossover(close, lower)

long_condition  = cross_above_upper and hour_ok and (not use_macd or macd_bull)
short_condition = cross_below_lower and hour_ok and (not use_macd or macd_bear)
close_long_cond  = cross_back_upper
close_short_cond = cross_back_lower

// ── Strategy Orders ────────────────────────────────────────── //
if long_condition
    strategy.close("Short", comment="reverse")
    strategy.entry("Long", strategy.long, comment="gc_long")
    alert('{alert_buy}', alert.freq_once_per_bar_close)

if short_condition
    strategy.close("Long", comment="reverse")
    strategy.entry("Short", strategy.short, comment="gc_short")
    alert('{alert_sell}', alert.freq_once_per_bar_close)

if strategy.position_size > 0 and close_long_cond and not long_condition
    strategy.close("Long", comment="band_exit")
    alert('{alert_close}', alert.freq_once_per_bar_close)

if strategy.position_size < 0 and close_short_cond and not short_condition
    strategy.close("Short", comment="band_exit")
    alert('{alert_close}', alert.freq_once_per_bar_close)

// ── Stop-Loss / Take-Profit ────────────────────────────────── //
if sl_pct > 0
    if strategy.position_size > 0
        tp_price = tp_pct > 0 ? strategy.position_avg_price * (1 + tp_pct/100) : na
        strategy.exit("SL/TP Long",  "Long",  stop=strategy.position_avg_price*(1-sl_pct/100), limit=tp_price)
    if strategy.position_size < 0
        tp_price = tp_pct > 0 ? strategy.position_avg_price * (1 - tp_pct/100) : na
        strategy.exit("SL/TP Short", "Short", stop=strategy.position_avg_price*(1+sl_pct/100), limit=tp_price)

// ── Visuals ────────────────────────────────────────────────── //
plotshape(long_condition,  "Long Signal",  shape.triangleup,   location.belowbar, color.green, size=size.small)
plotshape(short_condition, "Short Signal", shape.triangledown, location.abovebar, color.red,   size=size.small)
"""


# ──────────────────────────────────────────────────────────────────────────────
# MA Cross PineScript
# ──────────────────────────────────────────────────────────────────────────────

def _pine_ma_cross(
    params: Dict[str, Any],
    symbol: str,
    initial_capital: float,
    commission_pct: float,
    slippage_ticks: int,
    bot_id: str,
) -> str:
    fast = int(params.get("fast_period", 20))
    slow = int(params.get("slow_period", 50))
    sl   = float(params.get("sl_pct", 0.03)) * 100

    alert_buy  = f'{{"bot_id":"{bot_id}","ticker":"{{{{ticker}}}}","action":"buy","order_size":"100%","position_size":"1","timestamp":"{{{{timenow}}}}","schema":"2"}}'
    alert_sell = f'{{"bot_id":"{bot_id}","ticker":"{{{{ticker}}}}","action":"sell","order_size":"100%","position_size":"-1","timestamp":"{{{{timenow}}}}","schema":"2"}}'

    return f"""//@version=5
// Lighthouse Trading — MA Cross Strategy
// Auto-generated by backtester/pine_generator.py

strategy("Lighthouse MA Cross", overlay=true,
  initial_capital={int(initial_capital)}, default_qty_type=strategy.percent_of_equity,
  default_qty_value=100, commission_type=strategy.commission.percent,
  commission_value={commission_pct}, slippage={slippage_ticks})

fast_len = input.int({fast}, "Fast SMA", minval=2)
slow_len = input.int({slow}, "Slow SMA", minval=2)
sl_pct   = input.float({sl:.1f}, "Stop Loss %", minval=0.0, maxval=50.0)

fast_ma = ta.sma(close, fast_len)
slow_ma = ta.sma(close, slow_len)

plot(fast_ma, "Fast MA", color=color.blue,  linewidth=1)
plot(slow_ma, "Slow MA", color=color.orange, linewidth=2)

golden = ta.crossover(fast_ma, slow_ma)
death  = ta.crossunder(fast_ma, slow_ma)

if golden
    strategy.entry("Long", strategy.long)
    alert('{alert_buy}', alert.freq_once_per_bar_close)

if death
    strategy.entry("Short", strategy.short)
    alert('{alert_sell}', alert.freq_once_per_bar_close)

if sl_pct > 0
    if strategy.position_size > 0
        strategy.exit("SL Long",  "Long",  stop=strategy.position_avg_price*(1-sl_pct/100))
    if strategy.position_size < 0
        strategy.exit("SL Short", "Short", stop=strategy.position_avg_price*(1+sl_pct/100))
"""


# ──────────────────────────────────────────────────────────────────────────────
# Generic stub
# ──────────────────────────────────────────────────────────────────────────────

def _pine_stub(
    strategy: StrategyBase,
    symbol: str,
    initial_capital: float,
    commission_pct: float,
    slippage_ticks: int,
    bot_id: str,
) -> str:
    params_str = "\n".join(f"// {k} = {v}" for k, v in strategy.params.items())
    return f"""//@version=5
// Lighthouse Trading — {strategy.name} Strategy Stub
// Auto-generated by backtester/pine_generator.py
// This strategy does not have a full PineScript template yet.
// Parameters used in Python backtest:
{params_str}

strategy("{strategy.name}", overlay=true,
  initial_capital={int(initial_capital)},
  default_qty_type=strategy.percent_of_equity, default_qty_value=100,
  commission_type=strategy.commission.percent, commission_value={commission_pct},
  slippage={slippage_ticks})

// TODO: implement entry/exit logic for {strategy.name}
// Refer to backtester/strategies/{strategy.name}.py for the Python logic.

// Alert message schema (Signum v2):
// Buy:  {{"bot_id":"{bot_id}","ticker":"{{{{ticker}}}}","action":"buy","order_size":"100%","position_size":"1","timestamp":"{{{{timenow}}}}","schema":"2"}}
// Sell: {{"bot_id":"{bot_id}","ticker":"{{{{ticker}}}}","action":"sell","order_size":"100%","position_size":"-1","timestamp":"{{{{timenow}}}}","schema":"2"}}
"""
