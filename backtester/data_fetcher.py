"""
data_fetcher.py — Fetch OHLCV candles from Binance or Hyperliquid.

Features
--------
* Local cache in data/candles/ (Parquet format via pyarrow/fastparquet)
* Auto-update: only fetches new bars when cache exists
* Pagination: handles Binance 1000-bar limit and HL equivalent
* Supports: 1m, 5m, 15m, 1h, 4h, 1d timeframes

Usage
-----
    from backtester.data_fetcher import fetch_candles
    df = fetch_candles("BTCUSDT", "4h", "2023-01-01", "2026-01-01")
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

CANDLE_DIR = Path(__file__).resolve().parent.parent / "data" / "candles"

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
HL_CANDLE_URL = "https://api.hyperliquid.xyz/info"

BINANCE_TF_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
}

HL_TF_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
}

TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080,
}


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def fetch_candles(
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    source: str = "binance",
    use_futures: bool = True,
    use_cache: bool = True,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles and return a DataFrame.

    Parameters
    ----------
    symbol : str
        E.g. "BTCUSDT" (Binance) or "BTC" (Hyperliquid).
    timeframe : str
        One of: 1m, 5m, 15m, 1h, 4h, 1d (and other Binance intervals).
    start_date : str
        ISO date string, e.g. "2023-01-01".
    end_date : str
        ISO date string, e.g. "2026-01-01".
    source : str
        "binance" or "hyperliquid".
    use_futures : bool
        If True and source == "binance", use the USDT-M futures endpoint
        (includes funding rate).  Default True.
    use_cache : bool
        Enable local Parquet cache.  Default True.
    cache_dir : Path or None
        Override the default cache directory.

    Returns
    -------
    pd.DataFrame
        DatetimeIndex (UTC), columns: open, high, low, close, volume.
        Binance futures also includes: funding_rate (8h rate, forward-filled).
    """
    symbol    = symbol.upper()
    timeframe = timeframe.lower()
    start_dt  = pd.Timestamp(start_date, tz="UTC")
    end_dt    = pd.Timestamp(end_date,   tz="UTC")
    cache_dir = cache_dir or CANDLE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_path = cache_dir / f"{source}_{symbol}_{timeframe}.parquet"

    cached_df: Optional[pd.DataFrame] = None
    if use_cache and cache_path.exists():
        cached_df = _load_cache(cache_path)

    if cached_df is not None and not cached_df.empty:
        cache_start = cached_df.index.min()
        cache_end   = cached_df.index.max()

        # Need older data?
        if start_dt < cache_start:
            extra = _fetch_raw(symbol, timeframe, start_dt, cache_start, source, use_futures)
            if not extra.empty:
                cached_df = pd.concat([extra, cached_df])
                cached_df = cached_df[~cached_df.index.duplicated(keep="last")]
                cached_df.sort_index(inplace=True)

        # Need newer data?
        if end_dt > cache_end:
            extra = _fetch_raw(symbol, timeframe, cache_end, end_dt, source, use_futures)
            if not extra.empty:
                cached_df = pd.concat([cached_df, extra])
                cached_df = cached_df[~cached_df.index.duplicated(keep="last")]
                cached_df.sort_index(inplace=True)

        if use_cache:
            _save_cache(cached_df, cache_path)

        result = cached_df.loc[start_dt:end_dt]
    else:
        result = _fetch_raw(symbol, timeframe, start_dt, end_dt, source, use_futures)
        if use_cache and not result.empty:
            _save_cache(result, cache_path)

    if result.empty:
        logger.warning("No candles returned for %s %s %s → %s", symbol, timeframe, start_date, end_date)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Source-specific fetchers
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_raw(
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    source: str,
    use_futures: bool,
) -> pd.DataFrame:
    if source == "binance":
        return _fetch_binance(symbol, timeframe, start, end, use_futures)
    elif source == "hyperliquid":
        return _fetch_hyperliquid(symbol, timeframe, start, end)
    else:
        raise ValueError(f"Unknown source: {source!r}.  Choose 'binance' or 'hyperliquid'.")


# ─── Binance ──────────────────────────────────────────────────────────────────

def _fetch_binance(
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    use_futures: bool,
) -> pd.DataFrame:
    """Fetch from Binance with automatic pagination (1 000 bars / request)."""
    if timeframe not in BINANCE_TF_MAP:
        raise ValueError(f"Unsupported timeframe for Binance: {timeframe!r}")

    base_url = BINANCE_FUTURES_URL if use_futures else BINANCE_KLINES_URL
    interval = BINANCE_TF_MAP[timeframe]
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp()   * 1000)

    all_rows: list = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": current_start,
            "endTime":   end_ms,
            "limit":     1000,
        }
        try:
            resp = requests.get(base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Binance request failed: %s", exc)
            break

        if not data:
            break

        all_rows.extend(data)
        last_open_ms = data[-1][0]
        if last_open_ms >= end_ms or len(data) < 1000:
            break
        current_start = last_open_ms + 1
        time.sleep(0.1)  # be polite to the API

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    df.index.name = "timestamp"

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.sort_index(inplace=True)
    df = df[~df.index.duplicated(keep="last")]

    # Fetch funding rates if futures endpoint (8-hour rates)
    if use_futures:
        funding_df = _fetch_binance_funding(symbol, start, end)
        if not funding_df.empty:
            df = df.join(funding_df, how="left")
            df["funding_rate"] = df["funding_rate"].fillna(method="ffill").fillna(0.0)

    return df


def _fetch_binance_funding(
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Fetch Binance futures funding rate history."""
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp()   * 1000)
    all_rows: list = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol":    symbol,
            "startTime": current_start,
            "endTime":   end_ms,
            "limit":     1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        if not data:
            break

        all_rows.extend(data)
        if len(data) < 1000:
            break
        current_start = data[-1]["fundingTime"] + 1
        time.sleep(0.05)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df.set_index("timestamp", inplace=True)
    return df[["funding_rate"]]


# ─── Hyperliquid ──────────────────────────────────────────────────────────────

def _fetch_hyperliquid(
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Fetch candles from the Hyperliquid REST API."""
    if timeframe not in HL_TF_MAP:
        raise ValueError(f"Unsupported timeframe for Hyperliquid: {timeframe!r}")

    hl_interval = HL_TF_MAP[timeframe]
    tf_ms = TF_MINUTES.get(timeframe, 60) * 60 * 1000
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp()   * 1000)

    # HL returns up to 5 000 candles per request
    max_per_req = 5000
    all_rows: list = []
    current_start = start_ms

    while current_start < end_ms:
        current_end = min(current_start + tf_ms * max_per_req, end_ms)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin":       symbol,
                "interval":   hl_interval,
                "startTime":  current_start,
                "endTime":    current_end,
            },
        }
        try:
            resp = requests.post(HL_CANDLE_URL, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Hyperliquid request failed: %s", exc)
            break

        if not data:
            break

        all_rows.extend(data)
        if len(data) < max_per_req:
            break
        current_start = current_end + 1
        time.sleep(0.1)

    if not all_rows:
        return pd.DataFrame()

    records = []
    for bar in all_rows:
        records.append({
            "timestamp": pd.Timestamp(bar["t"], unit="ms", tz="UTC"),
            "open":   float(bar["o"]),
            "high":   float(bar["h"]),
            "low":    float(bar["l"]),
            "close":  float(bar["c"]),
            "volume": float(bar["v"]),
        })

    df = pd.DataFrame(records).set_index("timestamp")
    df.sort_index(inplace=True)
    df = df[~df.index.duplicated(keep="last")]
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────────────────────────────────────

def _save_cache(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
        logger.debug("Saved cache → %s (%d rows)", path.name, len(df))
    except Exception as exc:
        logger.warning("Failed to save cache: %s", exc)


def _load_cache(path: Path) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_parquet(path)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        logger.debug("Loaded cache ← %s (%d rows)", path.name, len(df))
        return df
    except Exception as exc:
        logger.warning("Failed to load cache %s: %s", path.name, exc)
        return None
