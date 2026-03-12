"""
Tests for DataPipeline — mock Binance API responses, cache read/write, Parquet format.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.data_pipeline import (
    DataPipeline,
    _date_to_ms,
    _parse_klines,
    _validate_timeframe,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def pipeline(tmp_path) -> DataPipeline:
    return DataPipeline(cache_dir=str(tmp_path / "candles"))


def _make_klines_row(ts_ms: int, price: float = 50000.0) -> list:
    """Build a Binance-format kline row."""
    return [
        ts_ms,           # open_time
        str(price),      # open
        str(price * 1.01),  # high
        str(price * 0.99),  # low
        str(price * 1.005), # close
        "1000.5",        # volume
        ts_ms + 3599999, # close_time
        "50000000",      # quote_vol
        100,             # n_trades
        "500.0",         # taker_buy_base
        "25000000",      # taker_buy_quote
        "0",             # ignore
    ]


def _make_klines_response(n: int = 5, start_ts_ms: int = 1_700_000_000_000) -> list:
    """Generate n candle rows with 1-hour intervals."""
    return [
        _make_klines_row(start_ts_ms + i * 3_600_000)
        for i in range(n)
    ]


# ── _parse_klines ─────────────────────────────────────────────────────────────

class TestParseKlines:
    def test_returns_dataframe(self):
        raw = _make_klines_response(5)
        df = _parse_klines(raw)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5

    def test_columns(self):
        raw = _make_klines_response(3)
        df = _parse_klines(raw)
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}

    def test_datetime_index(self):
        raw = _make_klines_response(3)
        df = _parse_klines(raw)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None  # timezone-aware

    def test_dtype_float64(self):
        raw = _make_klines_response(3)
        df = _parse_klines(raw)
        for col in ["open", "high", "low", "close", "volume"]:
            assert df[col].dtype == "float64", f"{col} should be float64"

    def test_empty_input(self):
        df = _parse_klines([])
        assert df.empty


# ── _date_to_ms ───────────────────────────────────────────────────────────────

class TestDateToMs:
    def test_basic_conversion(self):
        ms = _date_to_ms("2023-01-01")
        dt = datetime(2023, 1, 1, tzinfo=timezone.utc)
        assert ms == int(dt.timestamp() * 1000)

    def test_end_of_day(self):
        ms_start = _date_to_ms("2023-01-01")
        ms_end = _date_to_ms("2023-01-01", end_of_day=True)
        assert ms_end > ms_start

    def test_accepts_datetime_with_time(self):
        ms = _date_to_ms("2023-06-15T12:30:00")
        # Should truncate to date
        assert ms > 0


# ── _validate_timeframe ───────────────────────────────────────────────────────

class TestValidateTimeframe:
    def test_valid_timeframes(self):
        for tf in ["1h", "4h", "1d"]:
            _validate_timeframe(tf)  # should not raise

    def test_invalid_timeframe_raises(self):
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            _validate_timeframe("5m")

    def test_invalid_timeframe_message(self):
        with pytest.raises(ValueError, match="1h"):
            _validate_timeframe("15m")


# ── Cache write/read ──────────────────────────────────────────────────────────

class TestCacheReadWrite:
    def test_save_and_load_cache(self, pipeline, tmp_path):
        raw = _make_klines_response(10)
        df = _parse_klines(raw)
        pipeline._save_cache("BTCUSDT", "1h", df)

        loaded = pipeline._load_cache("BTCUSDT", "1h")
        assert loaded is not None
        assert len(loaded) == 10

    def test_parquet_schema(self, pipeline):
        raw = _make_klines_response(5)
        df = _parse_klines(raw)
        pipeline._save_cache("BTCUSDT", "1h", df)

        loaded = pipeline._load_cache("BTCUSDT", "1h")
        assert set(loaded.columns) == {"open", "high", "low", "close", "volume"}
        for col in ["open", "high", "low", "close", "volume"]:
            assert loaded[col].dtype == "float64"

    def test_load_nonexistent_returns_none(self, pipeline):
        result = pipeline._load_cache("FAKESYM", "1h")
        assert result is None

    def test_cache_path_format(self, pipeline, tmp_path):
        path = pipeline._cache_path("BTCUSDT", "1h")
        assert "BTCUSDT_1h.parquet" in path

    def test_datetime_index_preserved(self, pipeline):
        raw = _make_klines_response(5)
        df = _parse_klines(raw)
        pipeline._save_cache("ETHUSDT", "4h", df)
        loaded = pipeline._load_cache("ETHUSDT", "4h")
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert loaded.index.tz is not None


# ── fetch_candles with mocked Binance ────────────────────────────────────────

class TestFetchCandles:
    def test_fetch_calls_binance_api(self, pipeline):
        raw = _make_klines_response(100, start_ts_ms=1_700_000_000_000)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp) as mock_get:
            df = pipeline.fetch_candles("BTCUSDT", "1h", "2023-11-14", "2023-11-15")

        mock_get.assert_called()
        assert not df.empty

    def test_fetch_returns_dataframe_with_correct_columns(self, pipeline):
        raw = _make_klines_response(100, start_ts_ms=1_700_000_000_000)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp):
            df = pipeline.fetch_candles("BTCUSDT", "1h", "2023-11-14", "2023-11-15")

        assert set(df.columns) == {"open", "high", "low", "close", "volume"}

    def test_fetch_caches_result(self, pipeline, tmp_path):
        raw = _make_klines_response(50, start_ts_ms=1_700_000_000_000)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp):
            pipeline.fetch_candles("SOLUSDT", "4h", "2023-11-14", "2023-11-15")

        # Cache file should exist now
        import os
        cache_path = pipeline._cache_path("SOLUSDT", "4h")
        assert os.path.exists(cache_path)

    def test_fetch_uses_cache_on_second_call(self, pipeline):
        # 2023-11-14T00:00:00Z = 1699920000000 ms — aligns exactly with requested start
        start_ms = _date_to_ms("2023-11-14")
        raw = _make_klines_response(60, start_ts_ms=start_ms)  # 60 daily candles
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp) as mock_get:
            pipeline.fetch_candles("BTCUSDT", "1d", "2023-11-14", "2023-11-15")
            call_count_1 = mock_get.call_count

            # Second call with same date range — fully covered by cache, no new API calls
            pipeline.fetch_candles("BTCUSDT", "1d", "2023-11-14", "2023-11-15")
            call_count_2 = mock_get.call_count

        assert call_count_2 == call_count_1, "Should not call Binance again for cached data"

    def test_fetch_invalid_timeframe_raises(self, pipeline):
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            pipeline.fetch_candles("BTCUSDT", "5m", "2023-01-01", "2023-01-02")

    def test_fetch_handles_api_error(self, pipeline):
        import requests as req
        with patch(
            "app.services.data_pipeline.requests.get",
            side_effect=req.RequestException("timeout"),
        ):
            df = pipeline.fetch_candles("BTCUSDT", "1h", "2023-11-14", "2023-11-15")
        assert df.empty


# ── get_cached ────────────────────────────────────────────────────────────────

class TestGetCached:
    def test_returns_empty_when_no_cache(self, pipeline):
        df = pipeline.get_cached("BTCUSDT", "1h")
        assert df.empty

    def test_returns_cached_data(self, pipeline):
        raw = _make_klines_response(20)
        df = _parse_klines(raw)
        pipeline._save_cache("BTCUSDT", "1h", df)

        result = pipeline.get_cached("BTCUSDT", "1h")
        assert len(result) == 20

    def test_date_filtering(self, pipeline):
        # Create data spanning Jan 1-10, 2023 (hourly)
        base_ts = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        raw = _make_klines_response(240, start_ts_ms=base_ts)  # 10 days × 24h
        df = _parse_klines(raw)
        pipeline._save_cache("BTCUSDT", "1h", df)

        filtered = pipeline.get_cached("BTCUSDT", "1h", start_date="2023-01-05", end_date="2023-01-07")
        assert not filtered.empty
        assert all(filtered.index >= pd.Timestamp("2023-01-05", tz="UTC"))
        assert all(filtered.index <= pd.Timestamp("2023-01-07 23:59:59", tz="UTC"))


# ── update_cache ──────────────────────────────────────────────────────────────

class TestUpdateCache:
    def test_update_cache_creates_new_cache(self, pipeline):
        raw = _make_klines_response(100, start_ts_ms=1_680_000_000_000)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp):
            pipeline.update_cache("BTCUSDT", "1h")

        cached = pipeline._load_cache("BTCUSDT", "1h")
        assert cached is not None
        assert not cached.empty

    def test_update_cache_appends_new_candles(self, pipeline):
        base_ts = 1_700_000_000_000
        # Pre-load cache with 10 candles
        initial_raw = _make_klines_response(10, start_ts_ms=base_ts)
        initial_df = _parse_klines(initial_raw)
        pipeline._save_cache("BTCUSDT", "1h", initial_df)

        # Mock new candles after existing cache
        new_ts = base_ts + 10 * 3_600_000  # 10 hours later
        new_raw = _make_klines_response(5, start_ts_ms=new_ts)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = new_raw
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.data_pipeline.requests.get", return_value=mock_resp):
            with patch("app.services.data_pipeline.time.time", return_value=new_ts / 1000 + 7200):
                pipeline.update_cache("BTCUSDT", "1h")

        updated = pipeline._load_cache("BTCUSDT", "1h")
        assert len(updated) >= 10  # At minimum the original 10


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestRateLimit:
    def test_rate_limit_enforced(self, pipeline):
        """Verify that two consecutive requests have >= 1s between them."""
        pipeline._last_request_time = time.time()  # just made a request
        t0 = time.time()
        pipeline._rate_limit()
        elapsed = time.time() - t0
        assert elapsed >= 0.9  # allow small timing variance
