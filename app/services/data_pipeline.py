"""
Lighthouse Trading - Data Pipeline
Fetches OHLCV candles from Binance public REST API and caches them as Parquet.

Cache layout
------------
data/candles/{symbol}_{timeframe}.parquet

Parquet schema
--------------
timestamp  datetime64[ns, UTC]  (index)
open       float64
high       float64
low        float64
close      float64
volume     float64

Rate limit: max 1 request per second to Binance.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_MAX_CANDLES_PER_REQUEST = 1000   # Binance limit
_MIN_REQUEST_INTERVAL = 1.0       # seconds between requests

_SUPPORTED_TIMEFRAMES: Dict[str, int] = {
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DEFAULT_TIMEFRAMES = ["1h", "4h", "1d"]
DEFAULT_CACHE_DIR = "data/candles"

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


# ── Pipeline ──────────────────────────────────────────────────────────────────

class DataPipeline:
    """
    Automated candle data fetching and caching for the backtester.

    All methods are synchronous and safe to call from CLI scripts or
    background threads.
    """

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._last_request_time: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles for symbol/timeframe between start_date and end_date.
        First checks cache; fetches missing ranges from Binance.

        Parameters
        ----------
        symbol      : e.g. "BTCUSDT"
        timeframe   : "1h" | "4h" | "1d"
        start_date  : "YYYY-MM-DD"
        end_date    : "YYYY-MM-DD"

        Returns
        -------
        DataFrame with DatetimeIndex (UTC) and columns: open, high, low, close, volume
        """
        symbol = symbol.upper()
        _validate_timeframe(timeframe)

        start_ts = _date_to_ms(start_date)
        end_ts = _date_to_ms(end_date, end_of_day=True)

        cached = self._load_cache(symbol, timeframe)

        if cached is not None and not cached.empty:
            cached_start = int(cached.index[0].timestamp() * 1000)
            cached_end = int(cached.index[-1].timestamp() * 1000)

            pieces: List[pd.DataFrame] = []

            # Fetch before cached range
            if start_ts < cached_start:
                pre = self._fetch_range(symbol, timeframe, start_ts, cached_start - 1)
                if not pre.empty:
                    pieces.append(pre)

            pieces.append(cached)

            # Fetch after cached range
            interval_ms = _SUPPORTED_TIMEFRAMES[timeframe]
            next_after_cache = cached_end + interval_ms
            if end_ts > cached_end:
                post = self._fetch_range(symbol, timeframe, next_after_cache, end_ts)
                if not post.empty:
                    pieces.append(post)

            if len(pieces) > 1:
                combined = pd.concat(pieces)
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                self._save_cache(symbol, timeframe, combined)
                df = combined
            else:
                df = cached
        else:
            df = self._fetch_range(symbol, timeframe, start_ts, end_ts)
            if not df.empty:
                self._save_cache(symbol, timeframe, df)

        if df.empty:
            return df

        # Slice to requested range
        start_dt = pd.Timestamp(start_ts, unit="ms", tz="UTC")
        end_dt = pd.Timestamp(end_ts, unit="ms", tz="UTC")
        return df.loc[start_dt:end_dt]

    def update_cache(self, symbol: str, timeframe: str) -> None:
        """
        Append new candles to the existing cache (up to now).
        Creates cache if it doesn't exist yet.
        """
        symbol = symbol.upper()
        _validate_timeframe(timeframe)

        cached = self._load_cache(symbol, timeframe)
        interval_ms = _SUPPORTED_TIMEFRAMES[timeframe]
        now_ms = int(time.time() * 1000)

        if cached is not None and not cached.empty:
            # Start from the candle after the last cached one
            last_ms = int(cached.index[-1].timestamp() * 1000)
            start_ms = last_ms + interval_ms
        else:
            # Default: fetch last 2 years
            start_ms = now_ms - 2 * 365 * 86_400_000

        if start_ms >= now_ms:
            logger.info("%s %s cache already up to date", symbol, timeframe)
            return

        new_data = self._fetch_range(symbol, timeframe, start_ms, now_ms)
        if new_data.empty:
            logger.info("No new candles for %s %s", symbol, timeframe)
            return

        if cached is not None and not cached.empty:
            combined = pd.concat([cached, new_data])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
        else:
            combined = new_data

        self._save_cache(symbol, timeframe, combined)
        logger.info(
            "Updated cache %s %s: +%d candles (total %d)",
            symbol, timeframe, len(new_data), len(combined),
        )

    def get_cached(
        self,
        symbol: str,
        timeframe: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Return cached data for symbol/timeframe, optionally filtered by date range.
        Returns empty DataFrame if no cache exists.
        """
        symbol = symbol.upper()
        df = self._load_cache(symbol, timeframe)
        if df is None or df.empty:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        if start_date:
            start_dt = pd.Timestamp(_date_to_ms(start_date), unit="ms", tz="UTC")
            df = df.loc[df.index >= start_dt]
        if end_date:
            end_dt = pd.Timestamp(_date_to_ms(end_date, end_of_day=True), unit="ms", tz="UTC")
            df = df.loc[df.index <= end_dt]

        return df

    def update_all(
        self,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
    ) -> None:
        """Batch-update cache for all symbol × timeframe combinations."""
        symbols = symbols or DEFAULT_SYMBOLS
        timeframes = timeframes or DEFAULT_TIMEFRAMES

        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    self.update_cache(symbol, timeframe)
                except Exception as exc:
                    logger.error("update_all failed for %s %s: %s", symbol, timeframe, exc)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _fetch_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> pd.DataFrame:
        """Fetch all candles in [start_ms, end_ms] from Binance (handles pagination)."""
        interval_ms = _SUPPORTED_TIMEFRAMES[timeframe]
        pieces: List[pd.DataFrame] = []
        current_start = start_ms

        while current_start <= end_ms:
            batch = self._fetch_batch(symbol, timeframe, current_start, end_ms)
            if batch.empty:
                break
            pieces.append(batch)
            last_ts = int(batch.index[-1].timestamp() * 1000)
            current_start = last_ts + interval_ms

            if len(batch) < _MAX_CANDLES_PER_REQUEST:
                break  # no more data

        if not pieces:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        df = pd.concat(pieces)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        return df

    def _fetch_batch(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> pd.DataFrame:
        """Fetch a single batch (≤1000 candles) from Binance."""
        self._rate_limit()

        params = {
            "symbol": symbol,
            "interval": timeframe,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": _MAX_CANDLES_PER_REQUEST,
        }

        try:
            resp = requests.get(_BINANCE_KLINES_URL, params=params, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
        except requests.RequestException as exc:
            logger.error("Binance API error for %s %s: %s", symbol, timeframe, exc)
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        if not raw:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        return _parse_klines(raw)

    def _rate_limit(self) -> None:
        """Enforce minimum interval between Binance requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    def _cache_path(self, symbol: str, timeframe: str) -> str:
        return os.path.join(self.cache_dir, f"{symbol}_{timeframe}.parquet")

    def _load_cache(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(symbol, timeframe)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            return df
        except Exception as exc:
            logger.warning("Failed to load cache %s: %s", path, exc)
            return None

    def _save_cache(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, timeframe)
        try:
            df[_OHLCV_COLUMNS].to_parquet(path, index=True)
            logger.debug("Saved cache: %s (%d rows)", path, len(df))
        except Exception as exc:
            logger.error("Failed to save cache %s: %s", path, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_timeframe(timeframe: str) -> None:
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError(
            f"Unsupported timeframe '{timeframe}'. "
            f"Use one of: {', '.join(_SUPPORTED_TIMEFRAMES)}"
        )


def _date_to_ms(date_str: str, end_of_day: bool = False) -> int:
    """Convert 'YYYY-MM-DD' to millisecond Unix timestamp (UTC)."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _parse_klines(raw: list) -> pd.DataFrame:
    """
    Parse Binance klines response into a OHLCV DataFrame.

    Binance row: [open_time, open, high, low, close, volume, close_time,
                  quote_vol, n_trades, taker_buy_base, taker_buy_quote, ignore]
    """
    timestamps = [pd.Timestamp(row[0], unit="ms", tz="UTC") for row in raw]
    data = {
        "open":   [float(row[1]) for row in raw],
        "high":   [float(row[2]) for row in raw],
        "low":    [float(row[3]) for row in raw],
        "close":  [float(row[4]) for row in raw],
        "volume": [float(row[5]) for row in raw],
    }
    df = pd.DataFrame(data, index=pd.DatetimeIndex(timestamps, name="timestamp"))
    return df
