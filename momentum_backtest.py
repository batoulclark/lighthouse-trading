#!/usr/bin/env python3
"""
Momentum Day-Trading Backtester
3 Strategies × 5 Coins × Multiple SL/TP params
Data: Binance 5m/15m from 2025-01-01
Fee: 0.05%/side (Hyperliquid)
"""

import pandas as pd
import numpy as np
import requests
import time
import json
from datetime import datetime, timezone
from pathlib import Path

# ─── CONFIG ───
COINS = ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "ARBUSDT", "LINKUSDT"]
START_DATE = "2025-01-01"
FEE_PER_SIDE = 0.0005  # 0.05%
US_SESSION_START = 13  # UTC
US_SESSION_END = 21    # UTC
CACHE_DIR = Path("/tmp/binance_cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─── DATA FETCHING ───
def fetch_klines(symbol, interval, start_str, end_str=None):
    """Fetch klines from Binance with caching"""
    cache_file = CACHE_DIR / f"{symbol}_{interval}_{start_str.replace('-','')}.parquet"
    if cache_file.exists():
        df = pd.read_parquet(cache_file)
        if len(df) > 100:
            print(f"  [cache] {symbol} {interval}: {len(df)} bars")
            return df

    url = "https://api.binance.com/api/v3/klines"
    start_ts = int(pd.Timestamp(start_str, tz='UTC').timestamp() * 1000)
    end_ts = int(pd.Timestamp.now(tz='UTC').timestamp() * 1000) if end_str is None else int(pd.Timestamp(end_str, tz='UTC').timestamp() * 1000)

    all_data = []
    current = start_ts
    while current < end_ts:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current,
            "limit": 1000
        }
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code == 429:
                    time.sleep(10)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(2)

        if not data:
            break

        all_data.extend(data)
        current = data[-1][6] + 1  # next after close_time
        time.sleep(0.15)

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data, columns=[
        'open_time','open','high','low','close','volume',
        'close_time','quote_volume','trades','taker_buy_base',
        'taker_buy_quote','ignore'
    ])
    for col in ['open','high','low','close','volume','quote_volume']:
        df[col] = df[col].astype(float)
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df = df.set_index('timestamp')
    df = df[~df.index.duplicated(keep='first')]
    df.to_parquet(cache_file)
    print(f"  [fetched] {symbol} {interval}: {len(df)} bars")
    return df

# ─── INDICATORS ───
def add_indicators(df):
    """Add all needed indicators"""
    df = df.copy()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))

    # Volume average
    df['vol_avg20'] = df['volume'].rolling(20).mean()
    df['vol_ratio'] = df['volume'] / df['vol_avg20']

    # Session info
    df['hour'] = df.index.hour
    df['date'] = df.index.date

    return df.dropna()

# ─── STRATEGY 1: MOMENTUM SCALP (EMA cross + MACD + Volume) ───
def backtest_momentum_scalp(df, sl_pct, interval_minutes=5):
    """
    Enter on EMA9/21 cross + MACD histogram cross + volume > 1.5x avg
    US session only (13:00-21:00 UTC)
    Exit: reverse EMA cross OR 2hr max hold OR session end
    """
    trades = []
    in_trade = False
    entry_price = 0
    entry_time = None
    direction = 0  # 1=long, -1=short
    max_hold_bars = int(120 / interval_minutes)

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        current_time = df.index[i]
        hour = row['hour']

        # Close at session end
        if in_trade and (hour >= US_SESSION_END or hour < US_SESSION_START):
            exit_price = row['close']
            pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
            trades.append({
                'entry_time': entry_time, 'exit_time': current_time,
                'direction': 'long' if direction == 1 else 'short',
                'entry': entry_price, 'exit': exit_price,
                'pnl_pct': pnl * 100, 'exit_reason': 'session_end'
            })
            in_trade = False
            continue

        if in_trade:
            bars_held = i - entry_bar
            # Stop loss
            if direction == 1:
                dd = (row['low'] / entry_price - 1)
                if dd <= -sl_pct:
                    exit_price = entry_price * (1 - sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'long', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue
            else:
                dd = -(row['high'] / entry_price - 1)
                if dd <= -sl_pct:
                    exit_price = entry_price * (1 + sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'short', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue

            # Max hold
            if bars_held >= max_hold_bars:
                exit_price = row['close']
                pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    'entry_time': entry_time, 'exit_time': current_time,
                    'direction': 'long' if direction == 1 else 'short',
                    'entry': entry_price, 'exit': exit_price,
                    'pnl_pct': pnl * 100, 'exit_reason': 'max_hold'
                })
                in_trade = False
                continue

            # Reverse EMA cross exit
            if direction == 1 and row['ema9'] < row['ema21'] and prev['ema9'] >= prev['ema21']:
                exit_price = row['close']
                pnl = (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    'entry_time': entry_time, 'exit_time': current_time,
                    'direction': 'long', 'entry': entry_price, 'exit': exit_price,
                    'pnl_pct': pnl * 100, 'exit_reason': 'ema_reverse'
                })
                in_trade = False
                continue
            elif direction == -1 and row['ema9'] > row['ema21'] and prev['ema9'] <= prev['ema21']:
                exit_price = row['close']
                pnl = -(exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    'entry_time': entry_time, 'exit_time': current_time,
                    'direction': 'short', 'entry': entry_price, 'exit': exit_price,
                    'pnl_pct': pnl * 100, 'exit_reason': 'ema_reverse'
                })
                in_trade = False
                continue

        # Entry conditions - US session only
        if not in_trade and US_SESSION_START <= hour < US_SESSION_END - 1:
            # Check volume filter
            if row['vol_ratio'] < 1.5:
                continue

            # Bullish: EMA9 crosses above EMA21 + MACD hist crosses above 0
            if (prev['ema9'] <= prev['ema21'] and row['ema9'] > row['ema21'] and
                prev['macd_hist'] <= 0 and row['macd_hist'] > 0):
                in_trade = True
                direction = 1
                entry_price = row['close']
                entry_time = current_time
                entry_bar = i

            # Bearish: EMA9 crosses below EMA21 + MACD hist crosses below 0
            elif (prev['ema9'] >= prev['ema21'] and row['ema9'] < row['ema21'] and
                  prev['macd_hist'] >= 0 and row['macd_hist'] < 0):
                in_trade = True
                direction = -1
                entry_price = row['close']
                entry_time = current_time
                entry_bar = i

    return trades

# ─── STRATEGY 2: TREND CONTINUATION (EMA50 trend + pullback) ───
def backtest_trend_continuation(df, sl_pct=0.01, interval_minutes=15):
    """
    EMA50 on 15m as trend filter
    Enter on pullback to EMA21 + RSI bounce from 40 (long) or 60 (short)
    Hold up to 4 hours, close at session end
    """
    trades = []
    in_trade = False
    entry_price = 0
    entry_time = None
    direction = 0
    max_hold_bars = int(240 / interval_minutes)

    for i in range(2, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        prev2 = df.iloc[i-2]
        current_time = df.index[i]
        hour = row['hour']

        # Close at session end
        if in_trade and (hour >= US_SESSION_END or hour < US_SESSION_START):
            exit_price = row['close']
            pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
            trades.append({
                'entry_time': entry_time, 'exit_time': current_time,
                'direction': 'long' if direction == 1 else 'short',
                'entry': entry_price, 'exit': exit_price,
                'pnl_pct': pnl * 100, 'exit_reason': 'session_end'
            })
            in_trade = False
            continue

        if in_trade:
            bars_held = i - entry_bar
            # Stop loss
            if direction == 1:
                dd = row['low'] / entry_price - 1
                if dd <= -sl_pct:
                    exit_price = entry_price * (1 - sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'long', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue
            else:
                dd = -(row['high'] / entry_price - 1)
                if dd <= -sl_pct:
                    exit_price = entry_price * (1 + sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'short', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue

            # Max hold
            if bars_held >= max_hold_bars:
                exit_price = row['close']
                pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    'entry_time': entry_time, 'exit_time': current_time,
                    'direction': 'long' if direction == 1 else 'short',
                    'entry': entry_price, 'exit': exit_price,
                    'pnl_pct': pnl * 100, 'exit_reason': 'max_hold'
                })
                in_trade = False
                continue

        # Entry: US session, trend + pullback + RSI bounce
        if not in_trade and US_SESSION_START <= hour < US_SESSION_END - 2:
            # LONG: price above EMA50 (uptrend), pullback touched EMA21, RSI bounced from ~40
            if (row['close'] > row['ema50'] and
                prev['low'] <= prev['ema21'] * 1.002 and  # pullback near EMA21
                row['close'] > row['ema21'] and  # bounced back above
                prev['rsi'] <= 45 and row['rsi'] > prev['rsi'] and row['rsi'] >= 40):
                in_trade = True
                direction = 1
                entry_price = row['close']
                entry_time = current_time
                entry_bar = i

            # SHORT: price below EMA50 (downtrend), pullback to EMA21, RSI bounced from ~60
            elif (row['close'] < row['ema50'] and
                  prev['high'] >= prev['ema21'] * 0.998 and
                  row['close'] < row['ema21'] and
                  prev['rsi'] >= 55 and row['rsi'] < prev['rsi'] and row['rsi'] <= 60):
                in_trade = True
                direction = -1
                entry_price = row['close']
                entry_time = current_time
                entry_bar = i

    return trades

# ─── STRATEGY 3: VOLUME BREAKOUT ───
def backtest_volume_breakout(df, sl_pct, tp_pct, max_hold_minutes=60, interval_minutes=5):
    """
    Detect bars with volume > 2x 20-bar average
    Enter in direction of that bar's close
    Fixed SL/TP, max hold 30min-2hr
    """
    trades = []
    in_trade = False
    entry_price = 0
    max_hold_bars = int(max_hold_minutes / interval_minutes)

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        current_time = df.index[i]
        hour = row['hour']

        # Close at session end
        if in_trade and (hour >= US_SESSION_END or hour < US_SESSION_START):
            exit_price = row['close']
            pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
            trades.append({
                'entry_time': entry_time, 'exit_time': current_time,
                'direction': 'long' if direction == 1 else 'short',
                'entry': entry_price, 'exit': exit_price,
                'pnl_pct': pnl * 100, 'exit_reason': 'session_end'
            })
            in_trade = False
            continue

        if in_trade:
            bars_held = i - entry_bar

            if direction == 1:
                # Check TP
                if row['high'] / entry_price - 1 >= tp_pct:
                    exit_price = entry_price * (1 + tp_pct)
                    pnl = tp_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'long', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'take_profit'
                    })
                    in_trade = False
                    continue
                # Check SL
                if row['low'] / entry_price - 1 <= -sl_pct:
                    exit_price = entry_price * (1 - sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'long', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue
            else:
                # Check TP (short)
                if -(row['low'] / entry_price - 1) >= tp_pct:
                    exit_price = entry_price * (1 - tp_pct)
                    pnl = tp_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'short', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'take_profit'
                    })
                    in_trade = False
                    continue
                # Check SL (short)
                if row['high'] / entry_price - 1 >= sl_pct:
                    exit_price = entry_price * (1 + sl_pct)
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        'entry_time': entry_time, 'exit_time': current_time,
                        'direction': 'short', 'entry': entry_price, 'exit': exit_price,
                        'pnl_pct': pnl * 100, 'exit_reason': 'stop_loss'
                    })
                    in_trade = False
                    continue

            # Max hold
            if bars_held >= max_hold_bars:
                exit_price = row['close']
                pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    'entry_time': entry_time, 'exit_time': current_time,
                    'direction': 'long' if direction == 1 else 'short',
                    'entry': entry_price, 'exit': exit_price,
                    'pnl_pct': pnl * 100, 'exit_reason': 'max_hold'
                })
                in_trade = False
                continue

        # Entry: volume spike during US session
        if not in_trade and US_SESSION_START <= hour < US_SESSION_END - 1:
            if row['vol_ratio'] >= 2.0:
                bar_body = row['close'] - row['open']
                if abs(bar_body) > 0:
                    in_trade = True
                    direction = 1 if bar_body > 0 else -1
                    entry_price = row['close']
                    entry_time = current_time
                    entry_bar = i

    return trades


# ─── REPORTING ───
def summarize_trades(trades, strategy_name, params_str, coin):
    """Generate summary stats for a trade list"""
    if not trades:
        return {
            'strategy': strategy_name, 'coin': coin, 'params': params_str,
            'trades': 0, 'win_rate': 0, 'total_pnl': 0, 'avg_pnl': 0,
            'max_dd_trade': 0, 'best_trade': 0, 'sharpe': 0,
            'profit_factor': 0
        }

    pnls = [t['pnl_pct'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    avg_pnl = np.mean(pnls) if pnls else 0
    max_dd = min(pnls) if pnls else 0
    best = max(pnls) if pnls else 0
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_profit / gross_loss if gross_loss > 0 else 0

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t.get('exit_reason', 'unknown')
        reasons[r] = reasons.get(r, 0) + 1

    return {
        'strategy': strategy_name, 'coin': coin, 'params': params_str,
        'trades': len(trades), 'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 2), 'avg_pnl': round(avg_pnl, 3),
        'max_dd_trade': round(max_dd, 2), 'best_trade': round(best, 2),
        'sharpe': round(sharpe, 2), 'profit_factor': round(pf, 2),
        'exit_reasons': reasons
    }


def main():
    output_lines = []
    all_results = []

    def log(msg):
        print(msg)
        output_lines.append(msg)

    log("=" * 80)
    log("MOMENTUM DAY-TRADING BACKTEST")
    log(f"Period: {START_DATE} to present")
    log(f"Fee: {FEE_PER_SIDE*100}%/side | Session: {US_SESSION_START}:00-{US_SESSION_END}:00 UTC")
    log(f"Coins: {', '.join(COINS)}")
    log("=" * 80)

    # ─── FETCH ALL DATA ───
    log("\n📥 Fetching data...")
    data_5m = {}
    data_15m = {}
    for coin in COINS:
        log(f"  {coin}...")
        df5 = fetch_klines(coin, "5m", START_DATE)
        df15 = fetch_klines(coin, "15m", START_DATE)
        data_5m[coin] = add_indicators(df5) if len(df5) > 50 else pd.DataFrame()
        data_15m[coin] = add_indicators(df15) if len(df15) > 50 else pd.DataFrame()
        log(f"    5m: {len(data_5m[coin])} bars | 15m: {len(data_15m[coin])} bars")

    # ═══════════════════════════════════════════════════════════════
    # STRATEGY 1: MOMENTUM SCALP (5m data)
    # ═══════════════════════════════════════════════════════════════
    log("\n" + "=" * 80)
    log("STRATEGY 1: MOMENTUM SCALP (EMA9/21 + MACD + Volume)")
    log("Timeframe: 5m | Max hold: 2 hours | US session only")
    log("=" * 80)

    sl_options = [0.005, 0.01, 0.015]
    for coin in COINS:
        df = data_5m[coin]
        if df.empty:
            log(f"  {coin}: NO DATA")
            continue
        for sl in sl_options:
            trades = backtest_momentum_scalp(df, sl, interval_minutes=5)
            result = summarize_trades(trades, "Momentum Scalp", f"SL={sl*100}%", coin)
            all_results.append(result)
            log(f"  {coin} | SL={sl*100:.1f}% | Trades={result['trades']:4d} | "
                f"WR={result['win_rate']:5.1f}% | PnL={result['total_pnl']:+7.2f}% | "
                f"Avg={result['avg_pnl']:+.3f}% | PF={result['profit_factor']:.2f} | "
                f"Sharpe={result['sharpe']:.2f}")
            if result.get('exit_reasons'):
                reasons_str = " | ".join(f"{k}:{v}" for k,v in result['exit_reasons'].items())
                log(f"         Exits: {reasons_str}")

    # ═══════════════════════════════════════════════════════════════
    # STRATEGY 2: TREND CONTINUATION (15m data)
    # ═══════════════════════════════════════════════════════════════
    log("\n" + "=" * 80)
    log("STRATEGY 2: TREND CONTINUATION (EMA50 trend + pullback + RSI)")
    log("Timeframe: 15m | Max hold: 4 hours | US session only")
    log("=" * 80)

    sl_options_s2 = [0.005, 0.01, 0.015]
    for coin in COINS:
        df = data_15m[coin]
        if df.empty:
            log(f"  {coin}: NO DATA")
            continue
        for sl in sl_options_s2:
            trades = backtest_trend_continuation(df, sl, interval_minutes=15)
            result = summarize_trades(trades, "Trend Continuation", f"SL={sl*100}%", coin)
            all_results.append(result)
            log(f"  {coin} | SL={sl*100:.1f}% | Trades={result['trades']:4d} | "
                f"WR={result['win_rate']:5.1f}% | PnL={result['total_pnl']:+7.2f}% | "
                f"Avg={result['avg_pnl']:+.3f}% | PF={result['profit_factor']:.2f} | "
                f"Sharpe={result['sharpe']:.2f}")
            if result.get('exit_reasons'):
                reasons_str = " | ".join(f"{k}:{v}" for k,v in result['exit_reasons'].items())
                log(f"         Exits: {reasons_str}")

    # ═══════════════════════════════════════════════════════════════
    # STRATEGY 3: VOLUME BREAKOUT (5m data)
    # ═══════════════════════════════════════════════════════════════
    log("\n" + "=" * 80)
    log("STRATEGY 3: VOLUME BREAKOUT (2x volume spike)")
    log("Timeframe: 5m | US session only")
    log("=" * 80)

    vb_params = [
        (0.003, 0.005, 30),   # tight
        (0.005, 0.01, 60),    # medium
        (0.01, 0.015, 120),   # wide
        (0.003, 0.01, 60),    # tight SL, wide TP
        (0.005, 0.015, 120),  # medium SL, wide TP
    ]

    for coin in COINS:
        df = data_5m[coin]
        if df.empty:
            log(f"  {coin}: NO DATA")
            continue
        for sl, tp, mh in vb_params:
            trades = backtest_volume_breakout(df, sl, tp, max_hold_minutes=mh, interval_minutes=5)
            params = f"SL={sl*100}%/TP={tp*100}%/MH={mh}m"
            result = summarize_trades(trades, "Volume Breakout", params, coin)
            all_results.append(result)
            log(f"  {coin} | {params:25s} | Trades={result['trades']:4d} | "
                f"WR={result['win_rate']:5.1f}% | PnL={result['total_pnl']:+7.2f}% | "
                f"Avg={result['avg_pnl']:+.3f}% | PF={result['profit_factor']:.2f} | "
                f"Sharpe={result['sharpe']:.2f}")
            if result.get('exit_reasons'):
                reasons_str = " | ".join(f"{k}:{v}" for k,v in result['exit_reasons'].items())
                log(f"         Exits: {reasons_str}")

    # ═══════════════════════════════════════════════════════════════
    # LEVERAGE ANALYSIS for strategies with >15% return
    # ═══════════════════════════════════════════════════════════════
    log("\n" + "=" * 80)
    log("LEVERAGE ANALYSIS (for strategies with >15% cumulative return)")
    log("=" * 80)

    profitable = [r for r in all_results if r['total_pnl'] > 15]
    if not profitable:
        log("  No strategies exceeded 15% return threshold.")
        # Show top 5 anyway
        log("\n  Top 5 by total PnL:")
        top5 = sorted(all_results, key=lambda x: x['total_pnl'], reverse=True)[:5]
        for r in top5:
            log(f"    {r['strategy']:25s} | {r['coin']:8s} | {r['params']:30s} | PnL={r['total_pnl']:+7.2f}%")
    else:
        for r in profitable:
            base_pnl = r['total_pnl']
            log(f"\n  {r['strategy']} | {r['coin']} | {r['params']}")
            log(f"  Base (1x): PnL={base_pnl:+.2f}% | WR={r['win_rate']}% | PF={r['profit_factor']}")
            for lev in [2, 3]:
                lev_pnl = base_pnl * lev  # simplified (ignores liquidation)
                lev_dd = r['max_dd_trade'] * lev
                log(f"  {lev}x Lev:  PnL≈{lev_pnl:+.2f}% | Worst trade≈{lev_dd:+.2f}%")

    # ═══════════════════════════════════════════════════════════════
    # GRAND SUMMARY
    # ═══════════════════════════════════════════════════════════════
    log("\n" + "=" * 80)
    log("GRAND SUMMARY — TOP 15 CONFIGS")
    log("=" * 80)
    log(f"{'Strategy':25s} | {'Coin':8s} | {'Params':30s} | {'Trades':>6s} | {'WR%':>5s} | {'PnL%':>8s} | {'PF':>5s} | {'Sharpe':>6s}")
    log("-" * 110)

    top_configs = sorted(all_results, key=lambda x: x['total_pnl'], reverse=True)[:15]
    for r in top_configs:
        log(f"{r['strategy']:25s} | {r['coin']:8s} | {r['params']:30s} | {r['trades']:6d} | {r['win_rate']:5.1f} | {r['total_pnl']:+8.2f} | {r['profit_factor']:5.2f} | {r['sharpe']:6.2f}")

    log("\n" + "=" * 80)
    log("BOTTOM 10 (WORST PERFORMERS)")
    log("=" * 80)
    bottom = sorted(all_results, key=lambda x: x['total_pnl'])[:10]
    for r in bottom:
        log(f"{r['strategy']:25s} | {r['coin']:8s} | {r['params']:30s} | {r['trades']:6d} | {r['win_rate']:5.1f} | {r['total_pnl']:+8.2f} | {r['profit_factor']:5.2f}")

    # ─── KEY TAKEAWAYS ───
    log("\n" + "=" * 80)
    log("KEY OBSERVATIONS")
    log("=" * 80)

    # Best per strategy
    for strat in ["Momentum Scalp", "Trend Continuation", "Volume Breakout"]:
        strat_results = [r for r in all_results if r['strategy'] == strat]
        if strat_results:
            best = max(strat_results, key=lambda x: x['total_pnl'])
            worst = min(strat_results, key=lambda x: x['total_pnl'])
            avg_pnl = np.mean([r['total_pnl'] for r in strat_results])
            log(f"\n  {strat}:")
            log(f"    Best:  {best['coin']} {best['params']} → {best['total_pnl']:+.2f}% ({best['trades']} trades, WR={best['win_rate']}%)")
            log(f"    Worst: {worst['coin']} {worst['params']} → {worst['total_pnl']:+.2f}%")
            log(f"    Avg across all coins/params: {avg_pnl:+.2f}%")

    # Overall verdict
    any_positive = any(r['total_pnl'] > 0 and r['trades'] >= 20 for r in all_results)
    if any_positive:
        viable = [r for r in all_results if r['total_pnl'] > 0 and r['trades'] >= 20]
        log(f"\n  ✅ {len(viable)} configurations showed positive returns with ≥20 trades")
    else:
        log(f"\n  ❌ No configurations showed positive returns with sufficient trade count")

    log("\n" + "=" * 80)

    # Save
    with open("/tmp/subagent_momentum_results.txt", "w") as f:
        f.write("\n".join(output_lines))
    print(f"\n✅ Results saved to /tmp/subagent_momentum_results.txt")

if __name__ == "__main__":
    main()
