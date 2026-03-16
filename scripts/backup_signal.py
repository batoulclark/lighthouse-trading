#!/usr/bin/env python3
"""
Lighthouse Trading — Backup Signal Generator
Runs at 00:05 UTC daily via cron.

1. Fetches BTC daily candle from Binance
2. Calculates Gaussian Channel + all indicators
3. Determines if a signal should have fired
4. Checks if Lighthouse already received a TV webhook
5. If TV missed → fires the signal to Lighthouse directly
6. Sends verification report to Foufi

Same logic as Gaussian L+S v6 PineScript.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import numpy as np
import pandas as pd

# ── Config ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_FILE = PROJECT_ROOT / "data" / "trades.json"
STATE_FILE = PROJECT_ROOT / "data" / "backup_signal_state.json"
LOG_FILE = PROJECT_ROOT / "data" / "backup_signal.log"

# Lighthouse webhook
LIGHTHOUSE_URL = "http://localhost:8420/webhook/0d670f2b-5d55-4b5b-ad0f-6860c872193e"
WEBHOOK_SECRET = "1ecf4d43-346c-4f8e-847b-5e7dff4b727b"

# Telegram (Foufi)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8785179286:AAHO6zpcyl5v2SEo6NgIcxUAZ9AE9vX0xmA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7422563444")

# Strategy params (must match v6 PineScript exactly)
GC_PERIOD = 25
GC_MULT = 0.8
SMA_LONG = 200
SMA_SHORT = 150
EMA_PERIOD = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9
TEX = 2
RE_BAND = 0.4
HOLD_MIN_L = 5
HOLD_MIN_S = 7
FAST_RE_BARS = 10
TRAIL_ACT = 15.0
TRAIL_PCT = 5.0


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_telegram(text: str):
    """Send message via Foufi bot."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
        if resp.status_code != 200:
            log(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log(f"Telegram error: {e}")


def fetch_daily_candles(symbol="BTCUSDT", days=250):
    """Fetch enough daily candles for SMA200 + buffer."""
    url = "https://api.binance.com/api/v3/klines"
    all_data = []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (days * 24 * 3600 * 1000)

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if not data:
            break
        all_data.extend(data)
        start_ms = data[-1][0] + 1
        if len(data) < 1000:
            break

    df = pd.DataFrame(all_data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df.set_index("open_time")
    return df


def compute_gaussian_channel(df):
    """Compute Gaussian Channel + all indicators."""
    beta = (1 - math.cos(2 * math.pi / GC_PERIOD)) / (2**1.0 - 1)
    alpha = -beta + math.sqrt(beta**2 + 2 * beta)

    hlc3 = (df["high"] + df["low"] + df["close"]) / 3
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs()
    ], axis=1).max(axis=1)

    gc = np.zeros(len(df))
    gc_tr = np.zeros(len(df))
    gc[0:4] = hlc3.iloc[0:4].values
    gc_tr[0:4] = tr.iloc[0:4].values

    for i in range(4, len(df)):
        gc[i] = alpha**2 * hlc3.iloc[i] + 2*(1-alpha)*gc[i-1] - (1-alpha)**2 * gc[i-2]
        gc_tr[i] = alpha**2 * tr.iloc[i] + 2*(1-alpha)*gc_tr[i-1] - (1-alpha)**2 * gc_tr[i-2]

    df["gc"] = gc
    df["gc_upper"] = gc + GC_MULT * np.abs(gc_tr)
    df["gc_lower"] = gc - GC_MULT * np.abs(gc_tr)
    df["gc_green"] = df["gc"] > df["gc"].shift(1)

    df["sma200"] = df["close"].rolling(SMA_LONG).mean()
    df["sma150"] = df["close"].rolling(SMA_SHORT).mean()
    df["ema21"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()

    exp1 = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    exp2 = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=MACD_SIG, adjust=False).mean()
    df["hist"] = macd_line - signal_line

    return df


def load_state():
    """Load persistent state (position tracking)."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "position": None,  # {"side": "long"/"short", "entry_price": float, "bars": 0}
        "bars_since_long_exit": 999,
        "trail_active": False,
        "trail_peak": 0.0,
        "last_signal_date": None,
        "last_signal_action": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def determine_signal(df, state):
    """
    Determine what signal (if any) should fire on the latest candle.
    Returns: "buy", "sell", "close_long", "close_short", or None
    """
    if len(df) < SMA_LONG + 5:
        return None, "Not enough data"

    i = len(df) - 1
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    close = row["close"]

    gc_green_now = row["gc_green"]
    gc_green_prev = df.iloc[i-1]["gc_green"]
    gc_green_prev2 = df.iloc[i-2]["gc_green"]

    above_sma = close > row["sma200"] if not pd.isna(row["sma200"]) else False
    below_sma = close < row["sma150"] if not pd.isna(row["sma150"]) else False

    ch_w = row["gc_upper"] - row["gc_lower"]
    band_pos = (close - row["gc_lower"]) / ch_w if ch_w > 0 else 0.5

    hist_now = row["hist"]
    hist_prev = prev["hist"]
    macd_cross_up = hist_now > 0 and hist_prev <= 0

    pos = state.get("position")
    bsle = state.get("bars_since_long_exit", 999)

    # ── Exit checks ──
    if pos:
        bars = pos.get("bars", 0)

        # Trail stop
        if state.get("trail_active") and TRAIL_ACT > 0:
            tp = state.get("trail_peak", 0)
            if pos["side"] == "long":
                if close > tp:
                    state["trail_peak"] = close
                    tp = close
                if close <= tp * (1 - TRAIL_PCT / 100):
                    return "close_long", f"Trail stop triggered (peak ${tp:,.0f}, close ${close:,.0f})"
            else:
                if close < tp:
                    state["trail_peak"] = close
                    tp = close
                if close >= tp * (1 + TRAIL_PCT / 100):
                    return "close_short", f"Trail stop triggered (trough ${tp:,.0f}, close ${close:,.0f})"

        # Trail activation
        if not state.get("trail_active") and TRAIL_ACT > 0:
            if pos["side"] == "long":
                upct = (close - pos["entry_price"]) / pos["entry_price"] * 100
            else:
                upct = (pos["entry_price"] - close) / pos["entry_price"] * 100
            if upct >= TRAIL_ACT:
                state["trail_active"] = True
                state["trail_peak"] = close

        # Long exit
        if pos["side"] == "long" and bars >= HOLD_MIN_L:
            grc = sum(1 for j in range(TEX) if i-j >= 0 and not df.iloc[i-j]["gc_green"])
            sma_cross_dn = not above_sma and not pd.isna(df.iloc[i-1]["sma200"]) and df.iloc[i-1]["close"] >= df.iloc[i-1]["sma200"]
            if grc >= TEX or sma_cross_dn:
                reason = "GC red" if grc >= TEX else "SMA200 cross down"
                return "close_long", reason

        # Short exit
        if pos["side"] == "short" and bars >= HOLD_MIN_S:
            ema_cross_up = close > row["ema21"] and prev["close"] <= prev["ema21"]
            sma_cross_up = above_sma and not pd.isna(df.iloc[i-1]["sma200"]) and df.iloc[i-1]["close"] <= df.iloc[i-1]["sma200"]
            if macd_cross_up or ema_cross_up or sma_cross_up:
                reason = "MACD cross" if macd_cross_up else ("EMA21 cross" if ema_cross_up else "SMA200 cross up")
                return "close_short", reason

    # ── Entry checks (only if flat) ──
    if pos is None:
        # Long
        tt = gc_green_now and not gc_green_prev
        pb = gc_green_now and gc_green_prev and gc_green_prev2 and band_pos <= RE_BAND
        fre = FAST_RE_BARS > 0 and bsle <= FAST_RE_BARS and macd_cross_up

        if above_sma and (tt or pb or fre):
            trigger = "trend_turn" if tt else ("pullback" if pb else "fast_reentry")
            return "buy", f"Long entry ({trigger}) @ ${close:,.0f}"

        # Short
        if below_sma and close < row["ema21"] and prev["close"] >= prev["ema21"]:
            return "sell", f"Short entry (EMA21 break) @ ${close:,.0f}"

    return None, "No signal"


def check_tv_fired_today():
    """Check if TradingView already sent a webhook today."""
    if not TRADES_FILE.exists():
        return False
    try:
        with open(TRADES_FILE) as f:
            trades = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for t in reversed(trades):
            ts = t.get("timestamp", "")
            if ts.startswith(today):
                return True
    except Exception:
        pass
    return False


def fire_signal(action: str, price: float):
    """Send signal to Lighthouse webhook."""
    if action in ("buy", "sell"):
        position_size = "1" if action == "buy" else "-1"
    else:
        position_size = "0"
        action = "sell" if "long" in action else "buy"

    payload = {
        "bot_id": WEBHOOK_SECRET,
        "ticker": "BTCUSDT",
        "action": action,
        "order_size": "100%",
        "position_size": position_size,
        "price": price,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schema": "2",
        "comment": "backup_signal_generator",
    }

    try:
        resp = requests.post(LIGHTHOUSE_URL, json=payload, timeout=10)
        log(f"Fired backup signal: {action} → {resp.status_code} {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        log(f"Failed to fire signal: {e}")
        return False


def main():
    log("=" * 50)
    log("Backup Signal Generator — starting")

    # 1. Fetch data
    log("Fetching BTC daily candles from Binance...")
    df = fetch_daily_candles(days=250)
    log(f"Got {len(df)} candles, latest: {df.index[-1]}")

    # 2. Compute indicators
    df = compute_gaussian_channel(df)
    latest = df.iloc[-1]
    close = latest["close"]

    log(f"BTC close: ${close:,.2f}")
    log(f"SMA200: ${latest['sma200']:,.2f}" if not pd.isna(latest['sma200']) else "SMA200: N/A")
    log(f"GC green: {latest['gc_green']}")

    # 3. Load state & determine signal
    state = load_state()

    # Update bars if in position
    if state.get("position"):
        state["position"]["bars"] = state["position"].get("bars", 0) + 1
    state["bars_since_long_exit"] = state.get("bars_since_long_exit", 999) + 1

    signal, reason = determine_signal(df, state)
    log(f"Signal: {signal or 'NONE'} — {reason}")

    # 4. Check if TV already fired
    tv_fired = check_tv_fired_today()
    log(f"TV webhook today: {'YES' if tv_fired else 'NO'}")

    # 5. Act
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if signal is None:
        # No signal — just report
        pos_str = "FLAT"
        if state.get("position"):
            p = state["position"]
            pos_str = f"{p['side'].upper()} from ${p['entry_price']:,.0f} ({p['bars']}d)"

        send_telegram(
            f"🔦 *Backup Signal — {today_str}*\n"
            f"BTC: `${close:,.2f}`\n"
            f"Signal: None\n"
            f"Position: {pos_str}\n"
            f"TV webhook: {'✅' if tv_fired else '⚠️ No'}\n"
            f"Status: All clear"
        )

    elif signal and not tv_fired:
        # TV MISSED — fire backup signal
        log(f"⚠️ TV missed! Firing backup: {signal}")
        success = fire_signal(signal, close)

        send_telegram(
            f"🚨 *Backup Signal FIRED — {today_str}*\n"
            f"BTC: `${close:,.2f}`\n"
            f"Signal: *{signal.upper()}* — {reason}\n"
            f"TV webhook: ❌ MISSED\n"
            f"Backup fired: {'✅ Success' if success else '❌ Failed'}\n"
            f"⚠️ TradingView did not send a signal. Backup took over."
        )

        # Update state
        if signal == "buy":
            state["position"] = {"side": "long", "entry_price": close, "bars": 0}
            state["trail_active"] = False
            state["trail_peak"] = 0
        elif signal == "sell":
            state["position"] = {"side": "short", "entry_price": close, "bars": 0}
            state["trail_active"] = False
            state["trail_peak"] = 0
        elif "close" in signal:
            if "long" in signal:
                state["bars_since_long_exit"] = 0
            state["position"] = None
            state["trail_active"] = False
            state["trail_peak"] = 0

    elif signal and tv_fired:
        # TV already fired — just verify
        send_telegram(
            f"🔦 *Backup Signal — {today_str}*\n"
            f"BTC: `${close:,.2f}`\n"
            f"Signal: *{signal.upper()}* — {reason}\n"
            f"TV webhook: ✅ Already fired\n"
            f"Backup: Not needed — TV handled it"
        )

        # Still update state
        if signal == "buy":
            state["position"] = {"side": "long", "entry_price": close, "bars": 0}
            state["trail_active"] = False
        elif signal == "sell":
            state["position"] = {"side": "short", "entry_price": close, "bars": 0}
            state["trail_active"] = False
        elif "close" in signal:
            if "long" in signal:
                state["bars_since_long_exit"] = 0
            state["position"] = None
            state["trail_active"] = False

    state["last_signal_date"] = today_str
    state["last_signal_action"] = signal
    save_state(state)
    log("Done.\n")


if __name__ == "__main__":
    main()
