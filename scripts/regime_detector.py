#!/usr/bin/env python3
"""
Lighthouse Trading — Market Regime Detector
============================================
Fetches BTC daily OHLCV data, computes volatility / trend / volume metrics,
classifies the current market regime, and scores each active bot's strategy
fit for that regime.

Usage
-----
    python3 scripts/regime_detector.py [--days 60] [--api-url URL]

Output
------
    data/regime_status.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO_ROOT   = Path(__file__).resolve().parents[1]
_OUTPUT_FILE = _REPO_ROOT / "data" / "regime_status.json"
_BOTS_FILE   = _REPO_ROOT / "data" / "bots.json"

# ── Strategy regime fit map ───────────────────────────────────────────────────
# Maps strategy type keywords → regime → fit rating
# "gaussian" strategies: channel-based trend followers — love clean trends
# "roc" strategies: rate-of-change momentum — needs directional momentum
_STRATEGY_FIT: dict[str, dict[str, str]] = {
    "gaussian": {
        "trending_up":    "good",
        "trending_down":  "good",
        "range_bound":    "poor",
        "high_volatility":"moderate",
        "crash":          "poor",
    },
    "roc": {
        "trending_up":    "good",
        "trending_down":  "moderate",
        "range_bound":    "poor",
        "high_volatility":"moderate",
        "crash":          "poor",
    },
    "default": {
        "trending_up":    "moderate",
        "trending_down":  "moderate",
        "range_bound":    "moderate",
        "high_volatility":"poor",
        "crash":          "poor",
    },
}


# ── Binance public klines ─────────────────────────────────────────────────────

def _fetch_binance_klines(symbol: str = "BTCUSDT", days: int = 60) -> list[dict]:
    """
    Fetch daily OHLCV candles from Binance public API (no key required).
    Returns list of dicts: {date, open, high, low, close, volume}.
    Falls back gracefully on network error.
    """
    limit = min(days + 10, 200)   # buffer for SMA window
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1d&limit={limit}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw: list[list] = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Binance fetch failed: %s — trying fallback.", exc)
        return _fetch_yahoo_klines(symbol, days)

    candles = []
    for r in raw:
        ts_ms    = int(r[0])
        dt_str   = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        candles.append({
            "date":   dt_str,
            "open":   float(r[1]),
            "high":   float(r[2]),
            "low":    float(r[3]),
            "close":  float(r[4]),
            "volume": float(r[5]),
        })
    logger.info("Fetched %d candles from Binance (%s)", len(candles), symbol)
    return candles


def _fetch_yahoo_klines(symbol: str, days: int) -> list[dict]:
    """Fallback: Yahoo Finance daily OHLCV."""
    ticker = symbol.replace("USDT", "-USD")
    period = f"{days + 10}d"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&range={period}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        result   = data["chart"]["result"][0]
        ts_list  = result["timestamp"]
        q        = result["indicators"]["quote"][0]
        opens    = q.get("open",   [])
        highs    = q.get("high",   [])
        lows     = q.get("low",    [])
        closes   = q.get("close",  [])
        volumes  = q.get("volume", [])
        candles  = []
        for i, ts in enumerate(ts_list):
            try:
                c = closes[i]
                if c is None:
                    continue
                candles.append({
                    "date":   datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "open":   float(opens[i]  or c),
                    "high":   float(highs[i]  or c),
                    "low":    float(lows[i]   or c),
                    "close":  float(c),
                    "volume": float(volumes[i] or 0),
                })
            except (TypeError, IndexError):
                continue
        logger.info("Fetched %d candles from Yahoo (%s)", len(candles), ticker)
        return candles
    except Exception as exc:
        logger.error("Yahoo fallback also failed: %s", exc)
        return []


# ── Technical indicator helpers ───────────────────────────────────────────────

def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _realized_vol_30d(closes: list[float]) -> float | None:
    """
    30-day annualised realized volatility from daily log-returns.
    Returns None if insufficient data.
    """
    if len(closes) < 31:
        return None
    recent = closes[-31:]           # 30 returns from 31 closes
    log_returns = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
        if recent[i - 1] > 0 and recent[i] > 0
    ]
    if len(log_returns) < 5:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return round(daily_std * math.sqrt(365) * 100, 2)   # annualised %


def _trend_strength(prices: list[float]) -> dict[str, Any]:
    """
    Compare 20-day and 50-day SMAs to gauge trend.
    Returns dict with sma20, sma50, trend_pct (SMA20 vs SMA50 gap %).
    """
    sma20 = _sma(prices, 20)
    sma50 = _sma(prices, 50)
    if sma20 is None or sma50 is None:
        return {"sma20": None, "sma50": None, "trend_pct": None}
    trend_pct = round((sma20 - sma50) / sma50 * 100, 4)
    return {
        "sma20":      round(sma20, 2),
        "sma50":      round(sma50, 2),
        "trend_pct":  trend_pct,          # positive = bullish
    }


def _volume_profile(volumes: list[float]) -> dict[str, Any]:
    """
    Compare today's volume vs 30-day average.
    Returns dict with vol_today, vol_30d_avg, vol_ratio.
    """
    if len(volumes) < 2:
        return {"vol_today": None, "vol_30d_avg": None, "vol_ratio": None}
    recent_30 = volumes[-31:-1] if len(volumes) >= 31 else volumes[:-1]
    avg_30     = sum(recent_30) / len(recent_30)
    today_vol  = volumes[-1]
    vol_ratio  = round(today_vol / avg_30, 4) if avg_30 > 0 else None
    return {
        "vol_today":   round(today_vol, 2),
        "vol_30d_avg": round(avg_30, 2),
        "vol_ratio":   vol_ratio,
    }


# ── Regime classification ─────────────────────────────────────────────────────

def _classify_regime(
    current_price: float,
    sma20: float | None,
    sma50: float | None,
    vol_30d: float | None,
    closes_7d: list[float],
) -> str:
    """
    Classify market regime into one of:
      trending_up | trending_down | high_volatility | range_bound | crash
    Priority order: crash → high_volatility → trending_up → trending_down → range_bound
    """
    # ── Crash: >15% drop in last 7 days ───────────────────────────────────
    if len(closes_7d) >= 2:
        price_7d_ago = closes_7d[0]
        if price_7d_ago > 0:
            drop_pct = (current_price - price_7d_ago) / price_7d_ago * 100
            if drop_pct <= -15:
                return "crash"

    # ── High volatility: annualised realized vol > 80% ────────────────────
    if vol_30d is not None and vol_30d > 80:
        return "high_volatility"

    # Insufficient SMA data → range_bound fallback
    if sma20 is None or sma50 is None:
        return "range_bound"

    above_sma20 = current_price > sma20
    sma20_above_50 = sma20 > sma50

    # ── Trending up ───────────────────────────────────────────────────────
    if above_sma20 and sma20_above_50:
        # Low/medium vol confirms trend
        return "trending_up"

    # ── Trending down ─────────────────────────────────────────────────────
    if not above_sma20 and not sma20_above_50:
        return "trending_down"

    # ── Range-bound: mixed SMA signals, low vol ───────────────────────────
    return "range_bound"


# ── Strategy fit scoring ──────────────────────────────────────────────────────

def _infer_strategy_type(bot_name: str) -> str:
    name_lower = bot_name.lower()
    if "gaussian" in name_lower:
        return "gaussian"
    if "roc" in name_lower:
        return "roc"
    return "default"


def _score_strategies(regime: str, bots: list[dict]) -> list[dict]:
    """Return list of {strategy, current_regime_fit} for each active bot."""
    result = []
    for bot in bots:
        if not bot.get("enabled", True):
            continue
        strat_type = _infer_strategy_type(bot.get("name", ""))
        fit_map    = _STRATEGY_FIT.get(strat_type, _STRATEGY_FIT["default"])
        fit        = fit_map.get(regime, "moderate")
        result.append({
            "strategy":            bot.get("name", "unknown"),
            "strategy_type":       strat_type,
            "pair":                bot.get("pair", ""),
            "current_regime_fit":  fit,
        })
    return result


# ── Rolling regime history ─────────────────────────────────────────────────────

def _compute_history(candles: list[dict], lookback: int = 30) -> list[dict]:
    """Compute regime for each of the last `lookback` days."""
    history = []
    if len(candles) < 52:   # need 50 for SMA50 + at least 2
        return history

    recent = candles[-lookback:]
    for i, candle in enumerate(recent):
        # Index into full candles array
        idx = len(candles) - lookback + i
        slice_end = idx + 1
        if slice_end < 51:
            continue   # not enough history for SMA50
        hist_closes  = [c["close"]  for c in candles[:slice_end]]
        hist_volumes = [c["volume"] for c in candles[:slice_end]]

        sma20  = _sma(hist_closes, 20)
        sma50  = _sma(hist_closes, 50)
        vol30  = _realized_vol_30d(hist_closes)
        price  = hist_closes[-1]
        last7  = hist_closes[-7:] if len(hist_closes) >= 7 else hist_closes

        regime = _classify_regime(price, sma20, sma50, vol30, last7)
        history.append({"date": candle["date"], "regime": regime})

    return history


# ── Bots loader ───────────────────────────────────────────────────────────────

def _load_bots() -> list[dict]:
    if not _BOTS_FILE.exists():
        logger.warning("bots.json not found — strategy fit will be empty.")
        return []
    try:
        bots = json.loads(_BOTS_FILE.read_text())
        if isinstance(bots, list):
            return bots
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read bots.json: %s", exc)
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def run(days: int = 60) -> dict:
    """Run regime detection; return the output dict (also saved to JSON)."""
    logger.info("Fetching BTC daily candles (last ~%d days)…", days)
    candles = _fetch_binance_klines("BTCUSDT", days)

    if not candles:
        logger.error("No candle data available — cannot compute regime.")
        output = {
            "generated_at":  datetime.now(timezone.utc).isoformat(),
            "status":        "no_data",
            "message":       "Could not fetch price data from Binance or Yahoo.",
            "current_regime": None,
            "volatility_30d": None,
            "trend_strength": {},
            "volume_profile": {},
            "regime_history": [],
            "strategy_fit":  [],
        }
        _OUTPUT_FILE.write_text(json.dumps(output, indent=2))
        return output

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]

    current_price = closes[-1]
    current_date  = candles[-1]["date"]

    vol_30d  = _realized_vol_30d(closes)
    trend    = _trend_strength(closes)
    vol_prof = _volume_profile(volumes)
    last7_closes = closes[-7:] if len(closes) >= 7 else closes

    regime = _classify_regime(
        current_price,
        trend["sma20"],
        trend["sma50"],
        vol_30d,
        last7_closes,
    )

    logger.info(
        "Current regime: %s  |  BTC: $%.2f  |  Vol30d: %s%%  |  Trend: %s%%",
        regime,
        current_price,
        vol_30d,
        trend.get("trend_pct"),
    )

    bots          = _load_bots()
    strategy_fit  = _score_strategies(regime, bots)
    history       = _compute_history(candles, lookback=30)

    output = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "current_date":   current_date,
        "current_price":  round(current_price, 2),
        "current_regime": regime,
        "volatility_30d": vol_30d,
        "trend_strength": {
            "sma20":      trend["sma20"],
            "sma50":      trend["sma50"],
            "trend_pct":  trend["trend_pct"],
            "description": (
                "bullish alignment" if (trend["sma20"] or 0) > (trend["sma50"] or 0)
                else "bearish alignment"
            ) if trend["sma20"] and trend["sma50"] else "insufficient data",
        },
        "volume_profile": vol_prof,
        "regime_history": history,
        "strategy_fit":   strategy_fit,
    }

    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    logger.info("Saved regime status → %s", _OUTPUT_FILE)
    return output


# ── CLI entry-point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Market regime detector for Lighthouse Trading")
    parser.add_argument(
        "--days", type=int, default=60,
        help="Number of daily candles to fetch (default: 60)"
    )
    args = parser.parse_args()

    result = run(days=args.days)

    # Pretty print summary to stdout
    print("\n═══════════════════════════════════════════")
    print("  MARKET REGIME DETECTION")
    print("═══════════════════════════════════════════")
    print(f"  Date:           {result.get('current_date', 'N/A')}")
    print(f"  BTC Price:      ${result.get('current_price', 0):,.2f}")
    print(f"  Regime:         {result.get('current_regime', 'unknown').upper()}")
    print(f"  Volatility 30d: {result.get('volatility_30d', 'N/A')}%")
    ts = result.get("trend_strength", {})
    print(f"  SMA20:          ${ts.get('sma20', 'N/A')}")
    print(f"  SMA50:          ${ts.get('sma50', 'N/A')}")
    print(f"  Trend:          {ts.get('description', 'N/A')} ({ts.get('trend_pct', 'N/A')}%)")
    print()
    print("  STRATEGY FIT:")
    for s in result.get("strategy_fit", []):
        emoji = {"good": "✅", "moderate": "⚠️ ", "poor": "❌"}.get(s["current_regime_fit"], "?")
        print(f"  {emoji} {s['strategy']:<30} {s['current_regime_fit']}")
    print("═══════════════════════════════════════════\n")


if __name__ == "__main__":
    main()
