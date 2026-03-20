#!/usr/bin/env python3
"""
BB Squeeze Breakout + Ichimoku Cloud Day Trading Backtest
=========================================================
Strategy 1: Bollinger Band Squeeze Breakout (5m, 15m)
Strategy 2: Ichimoku Cloud Scalping (15m)

Coins: ETHUSDT, SOLUSDT, DOGEUSDT, ARBUSDT, LINKUSDT
Data: Binance API from 2025-01-01
Fee: 0.05%/side
"""

import sys
import pandas as pd
import numpy as np
import requests
import time
import json
from datetime import datetime, timezone
from io import StringIO

# ── CONFIG ──────────────────────────────────────────────
COINS = ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "ARBUSDT", "LINKUSDT"]
TIMEFRAMES = {"5m": 5, "15m": 15}
START_DATE = "2025-01-01"
END_DATE = "2025-03-18"
FEE_PER_SIDE = 0.0005  # 0.05%
US_SESSION_START = 13  # UTC
US_SESSION_END = 21    # UTC
BB_PERIOD = 20
BB_STD = 2
BB_WIDTH_AVG_PERIOD = 120
VOLUME_MULT = 1.5
SL_LEVELS = [0.005, 0.01, 0.02]  # 0.5%, 1%, 2%
MAX_HOLD_BARS_5M = 48   # 4 hours in 5m bars
MAX_HOLD_BARS_15M = 16  # 4 hours in 15m bars
ICHIMOKU_TENKAN = 9
ICHIMOKU_KIJUN = 26
ICHIMOKU_SENKOU = 52

RESULTS_FILE = "/tmp/subagent_bbsqueeze_results.txt"

# ── DATA DOWNLOAD ───────────────────────────────────────
def fetch_binance_klines(symbol, interval, start_str, end_str):
    """Fetch klines from Binance API with pagination."""
    url = "https://api.binance.com/api/v3/klines"
    start_ts = int(datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ts = int(datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    
    all_klines = []
    current_start = start_ts
    
    while current_start < end_ts:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ts,
            "limit": 1000,
        }
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(10)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 4:
                    print(f"  ERROR fetching {symbol} {interval}: {e}")
                    return pd.DataFrame()
                time.sleep(2)
        
        if not data:
            break
        
        all_klines.extend(data)
        current_start = data[-1][0] + 1
        time.sleep(0.15)  # rate limit
    
    if not all_klines:
        return pd.DataFrame()
    
    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = df[col].astype(float)
    
    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["hour"] = df["datetime"].dt.hour
    df = df.set_index("datetime")
    return df


# ── INDICATORS ──────────────────────────────────────────
def add_bollinger_bands(df):
    """Add BB(20,2) and squeeze detection."""
    df["bb_mid"] = df["close"].rolling(BB_PERIOD).mean()
    df["bb_std"] = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_STD * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["bb_width_avg"] = df["bb_width"].rolling(BB_WIDTH_AVG_PERIOD).mean()
    df["squeeze"] = df["bb_width"] < df["bb_width_avg"]
    df["squeeze_prev"] = df["squeeze"].shift(1)
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df


def add_ichimoku(df):
    """Add Ichimoku Cloud indicators."""
    high_9 = df["high"].rolling(ICHIMOKU_TENKAN).max()
    low_9 = df["low"].rolling(ICHIMOKU_TENKAN).min()
    df["tenkan"] = (high_9 + low_9) / 2
    
    high_26 = df["high"].rolling(ICHIMOKU_KIJUN).max()
    low_26 = df["low"].rolling(ICHIMOKU_KIJUN).min()
    df["kijun"] = (high_26 + low_26) / 2
    
    df["senkou_a"] = ((df["tenkan"] + df["kijun"]) / 2).shift(ICHIMOKU_KIJUN)
    
    high_52 = df["high"].rolling(ICHIMOKU_SENKOU).max()
    low_52 = df["low"].rolling(ICHIMOKU_SENKOU).min()
    df["senkou_b"] = ((high_52 + low_52) / 2).shift(ICHIMOKU_KIJUN)
    
    df["cloud_top"] = df[["senkou_a", "senkou_b"]].max(axis=1)
    df["cloud_bottom"] = df[["senkou_a", "senkou_b"]].min(axis=1)
    
    # Chikou span = close shifted back 26 periods
    df["chikou"] = df["close"].shift(-ICHIMOKU_KIJUN)
    # For comparison, we need price 26 bars ago
    df["price_26_ago"] = df["close"].shift(ICHIMOKU_KIJUN)
    
    return df


# ── STRATEGY 1: BB SQUEEZE BREAKOUT ────────────────────
def backtest_bb_squeeze(df, sl_pct, tf_minutes, symbol):
    """
    BB Squeeze Breakout backtest.
    - Enter when squeeze releases + big candle + volume confirm
    - Direction = direction of the breakout candle
    - SL = sl_pct from entry
    - Close at session end or max hold
    """
    max_hold = MAX_HOLD_BARS_5M if tf_minutes == 5 else MAX_HOLD_BARS_15M
    
    trades = []
    in_trade = False
    entry_price = 0
    direction = 0  # 1=long, -1=short
    entry_idx = 0
    entry_time = None
    
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        hour = row["hour"]
        
        # Close at session end
        if in_trade and (hour >= US_SESSION_END or hour < US_SESSION_START):
            exit_price = row["open"]  # exit at open of bar outside session
            pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
            trades.append({
                "entry_time": entry_time,
                "exit_time": df.index[i],
                "direction": "long" if direction == 1 else "short",
                "entry": entry_price,
                "exit": exit_price,
                "pnl_pct": pnl * 100,
                "exit_reason": "session_end",
                "bars_held": i - entry_idx,
            })
            in_trade = False
            continue
        
        # Skip if outside US session
        if hour < US_SESSION_START or hour >= US_SESSION_END:
            continue
        
        if in_trade:
            # Check SL
            if direction == 1:
                sl_price = entry_price * (1 - sl_pct)
                if row["low"] <= sl_price:
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": df.index[i],
                        "direction": "long",
                        "entry": entry_price,
                        "exit": sl_price,
                        "pnl_pct": pnl * 100,
                        "exit_reason": "stop_loss",
                        "bars_held": i - entry_idx,
                    })
                    in_trade = False
                    continue
            else:
                sl_price = entry_price * (1 + sl_pct)
                if row["high"] >= sl_price:
                    pnl = -sl_pct - 2 * FEE_PER_SIDE
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": df.index[i],
                        "direction": "short",
                        "entry": entry_price,
                        "exit": sl_price,
                        "pnl_pct": pnl * 100,
                        "exit_reason": "stop_loss",
                        "bars_held": i - entry_idx,
                    })
                    in_trade = False
                    continue
            
            # Check max hold
            if (i - entry_idx) >= max_hold:
                exit_price = row["close"]
                pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": df.index[i],
                    "direction": "long" if direction == 1 else "short",
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl_pct": pnl * 100,
                    "exit_reason": "max_hold",
                    "bars_held": i - entry_idx,
                })
                in_trade = False
                continue
        
        else:
            # Entry logic: squeeze was on, now releasing
            if (pd.notna(prev.get("squeeze")) and pd.notna(row.get("squeeze")) and
                pd.notna(prev.get("squeeze_prev"))):
                
                # Squeeze release: was in squeeze, now expanding
                was_squeeze = prev["squeeze"] == True
                now_expanding = row["squeeze"] == False
                
                if was_squeeze and now_expanding:
                    # Big candle = body > 0.5 * ATR or just direction of breakout
                    candle_body = row["close"] - row["open"]
                    candle_range = row["high"] - row["low"]
                    
                    # Volume confirm
                    vol_ok = row["volume"] > VOLUME_MULT * row["vol_avg"] if pd.notna(row["vol_avg"]) else False
                    
                    if vol_ok and candle_range > 0:
                        # Direction = direction of the breakout candle
                        if candle_body > 0:
                            direction = 1
                        elif candle_body < 0:
                            direction = -1
                        else:
                            continue
                        
                        entry_price = row["close"]
                        entry_idx = i
                        entry_time = df.index[i]
                        in_trade = True
    
    # Close any open trade at end
    if in_trade:
        exit_price = df.iloc[-1]["close"]
        pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
        trades.append({
            "entry_time": entry_time,
            "exit_time": df.index[-1],
            "direction": "long" if direction == 1 else "short",
            "entry": entry_price,
            "exit": exit_price,
            "pnl_pct": pnl * 100,
            "exit_reason": "end_of_data",
            "bars_held": len(df) - 1 - entry_idx,
        })
    
    return trades


# ── STRATEGY 2: ICHIMOKU CLOUD SCALPING ────────────────
def backtest_ichimoku(df, symbol):
    """
    Ichimoku Cloud Scalping on 15m.
    Long: price > cloud + tenkan > kijun + chikou > price_26_ago
    Short: opposite
    US session only, close at session end.
    SL: 1% (default for Ichimoku)
    """
    trades = []
    in_trade = False
    entry_price = 0
    direction = 0
    entry_idx = 0
    entry_time = None
    sl_pct = 0.01  # 1% SL for ichimoku
    max_hold = MAX_HOLD_BARS_15M
    
    for i in range(1, len(df)):
        row = df.iloc[i]
        hour = row["hour"]
        
        # Close at session end
        if in_trade and (hour >= US_SESSION_END or hour < US_SESSION_START):
            exit_price = row["open"]
            pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
            trades.append({
                "entry_time": entry_time,
                "exit_time": df.index[i],
                "direction": "long" if direction == 1 else "short",
                "entry": entry_price,
                "exit": exit_price,
                "pnl_pct": pnl * 100,
                "exit_reason": "session_end",
                "bars_held": i - entry_idx,
            })
            in_trade = False
            continue
        
        if hour < US_SESSION_START or hour >= US_SESSION_END:
            continue
        
        if in_trade:
            # SL check
            if direction == 1 and row["low"] <= entry_price * (1 - sl_pct):
                pnl = -sl_pct - 2 * FEE_PER_SIDE
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": df.index[i],
                    "direction": "long",
                    "entry": entry_price,
                    "exit": entry_price * (1 - sl_pct),
                    "pnl_pct": pnl * 100,
                    "exit_reason": "stop_loss",
                    "bars_held": i - entry_idx,
                })
                in_trade = False
                continue
            elif direction == -1 and row["high"] >= entry_price * (1 + sl_pct):
                pnl = -sl_pct - 2 * FEE_PER_SIDE
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": df.index[i],
                    "direction": "short",
                    "entry": entry_price,
                    "exit": entry_price * (1 + sl_pct),
                    "pnl_pct": pnl * 100,
                    "exit_reason": "stop_loss",
                    "bars_held": i - entry_idx,
                })
                in_trade = False
                continue
            
            # Max hold
            if (i - entry_idx) >= max_hold:
                exit_price = row["close"]
                pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": df.index[i],
                    "direction": "long" if direction == 1 else "short",
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl_pct": pnl * 100,
                    "exit_reason": "max_hold",
                    "bars_held": i - entry_idx,
                })
                in_trade = False
                continue
            
            # Reversal exit: if was long and now short signal (or vice versa)
            close = row["close"]
            tenkan = row["tenkan"]
            kijun = row["kijun"]
            cloud_top = row["cloud_top"]
            cloud_bottom = row["cloud_bottom"]
            
            if pd.notna(tenkan) and pd.notna(kijun) and pd.notna(cloud_top):
                if direction == 1 and close < cloud_bottom:
                    exit_price = close
                    pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": df.index[i],
                        "direction": "long",
                        "entry": entry_price,
                        "exit": exit_price,
                        "pnl_pct": pnl * 100,
                        "exit_reason": "reversal",
                        "bars_held": i - entry_idx,
                    })
                    in_trade = False
                    continue
                elif direction == -1 and close > cloud_top:
                    exit_price = close
                    pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": df.index[i],
                        "direction": "short",
                        "entry": entry_price,
                        "exit": exit_price,
                        "pnl_pct": pnl * 100,
                        "exit_reason": "reversal",
                        "bars_held": i - entry_idx,
                    })
                    in_trade = False
                    continue
        
        else:
            # Entry logic
            close = row["close"]
            tenkan = row.get("tenkan")
            kijun = row.get("kijun")
            cloud_top = row.get("cloud_top")
            cloud_bottom = row.get("cloud_bottom")
            price_26_ago = row.get("price_26_ago")
            
            if any(pd.isna(v) for v in [tenkan, kijun, cloud_top, cloud_bottom, price_26_ago]):
                continue
            
            # Long signal
            long_sig = (close > cloud_top and 
                       tenkan > kijun and 
                       close > price_26_ago)  # proxy for chikou > price
            
            # Short signal
            short_sig = (close < cloud_bottom and 
                        tenkan < kijun and 
                        close < price_26_ago)
            
            if long_sig:
                direction = 1
                entry_price = close
                entry_idx = i
                entry_time = df.index[i]
                in_trade = True
            elif short_sig:
                direction = -1
                entry_price = close
                entry_idx = i
                entry_time = df.index[i]
                in_trade = True
    
    # Close any open
    if in_trade:
        exit_price = df.iloc[-1]["close"]
        pnl = direction * (exit_price / entry_price - 1) - 2 * FEE_PER_SIDE
        trades.append({
            "entry_time": entry_time,
            "exit_time": df.index[-1],
            "direction": "long" if direction == 1 else "short",
            "entry": entry_price,
            "exit": exit_price,
            "pnl_pct": pnl * 100,
            "exit_reason": "end_of_data",
            "bars_held": len(df) - 1 - entry_idx,
        })
    
    return trades


# ── METRICS ─────────────────────────────────────────────
def compute_metrics(trades, label):
    """Compute standard backtest metrics."""
    if not trades:
        return {
            "label": label,
            "total_trades": 0,
            "win_rate": 0,
            "total_return_pct": 0,
            "avg_trade_pct": 0,
            "max_win_pct": 0,
            "max_loss_pct": 0,
            "profit_factor": 0,
            "avg_bars_held": 0,
            "sharpe": 0,
        }
    
    df_t = pd.DataFrame(trades)
    pnls = df_t["pnl_pct"].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    
    gross_profit = wins.sum() if len(wins) > 0 else 0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)
    
    # Compounded return
    comp_return = 1.0
    for p in pnls:
        comp_return *= (1 + p / 100)
    comp_return = (comp_return - 1) * 100
    
    # Sharpe (annualized rough estimate: assume ~2 trades/day, 252 trading days)
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 * 2)
    else:
        sharpe = 0
    
    return {
        "label": label,
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "total_return_pct": round(comp_return, 2),
        "avg_trade_pct": round(np.mean(pnls), 3),
        "max_win_pct": round(np.max(pnls), 2) if len(pnls) > 0 else 0,
        "max_loss_pct": round(np.min(pnls), 2) if len(pnls) > 0 else 0,
        "profit_factor": round(pf, 2),
        "avg_bars_held": round(np.mean(df_t["bars_held"].values), 1),
        "sharpe": round(sharpe, 2),
    }


def apply_leverage(trades, leverage):
    """Scale PnL by leverage."""
    levered = []
    for t in trades:
        lt = t.copy()
        lt["pnl_pct"] = t["pnl_pct"] * leverage
        levered.append(lt)
    return levered


# ── MAIN ────────────────────────────────────────────────
def main():
    output_lines = []
    
    def log(msg):
        print(msg, flush=True)
        output_lines.append(msg)
    
    log("=" * 80)
    log("BB SQUEEZE BREAKOUT + ICHIMOKU CLOUD BACKTEST")
    log(f"Period: {START_DATE} to {END_DATE}")
    log(f"Coins: {', '.join(COINS)}")
    log(f"Fee: {FEE_PER_SIDE*100}%/side")
    log("=" * 80)
    
    # Store all results for leverage testing
    all_results = []
    
    # ── Download data ───────────────────────────────────
    data_cache = {}
    for symbol in COINS:
        for tf in TIMEFRAMES:
            key = f"{symbol}_{tf}"
            log(f"\nFetching {symbol} {tf}...")
            df = fetch_binance_klines(symbol, tf, START_DATE, END_DATE)
            if df.empty:
                log(f"  WARNING: No data for {key}")
                continue
            log(f"  Got {len(df)} bars ({df.index[0]} to {df.index[-1]})")
            data_cache[key] = df
    
    # ── Strategy 1: BB Squeeze ──────────────────────────
    log("\n" + "=" * 80)
    log("STRATEGY 1: BOLLINGER BAND SQUEEZE BREAKOUT")
    log("=" * 80)
    
    for symbol in COINS:
        for tf, tf_min in TIMEFRAMES.items():
            key = f"{symbol}_{tf}"
            if key not in data_cache:
                continue
            
            df = data_cache[key].copy()
            df = add_bollinger_bands(df)
            
            for sl in SL_LEVELS:
                label = f"BB_Squeeze | {symbol} | {tf} | SL={sl*100}%"
                trades = backtest_bb_squeeze(df, sl, tf_min, symbol)
                metrics = compute_metrics(trades, label)
                all_results.append(metrics)
                
                log(f"\n{label}")
                log(f"  Trades: {metrics['total_trades']} | WR: {metrics['win_rate']}% | "
                    f"Return: {metrics['total_return_pct']}% | PF: {metrics['profit_factor']} | "
                    f"Sharpe: {metrics['sharpe']} | Avg Hold: {metrics['avg_bars_held']} bars")
                log(f"  Max Win: {metrics['max_win_pct']}% | Max Loss: {metrics['max_loss_pct']}%")
    
    # ── Strategy 2: Ichimoku Cloud ──────────────────────
    log("\n" + "=" * 80)
    log("STRATEGY 2: ICHIMOKU CLOUD SCALPING (15m only)")
    log("=" * 80)
    
    for symbol in COINS:
        key = f"{symbol}_15m"
        if key not in data_cache:
            continue
        
        df = data_cache[key].copy()
        df = add_ichimoku(df)
        
        label = f"Ichimoku | {symbol} | 15m | SL=1%"
        trades = backtest_ichimoku(df, symbol)
        metrics = compute_metrics(trades, label)
        all_results.append(metrics)
        
        log(f"\n{label}")
        log(f"  Trades: {metrics['total_trades']} | WR: {metrics['win_rate']}% | "
            f"Return: {metrics['total_return_pct']}% | PF: {metrics['profit_factor']} | "
            f"Sharpe: {metrics['sharpe']} | Avg Hold: {metrics['avg_bars_held']} bars")
        log(f"  Max Win: {metrics['max_win_pct']}% | Max Loss: {metrics['max_loss_pct']}%")
    
    # ── SUMMARY TABLE ───────────────────────────────────
    log("\n" + "=" * 80)
    log("SUMMARY TABLE — ALL CONFIGURATIONS (1x Leverage)")
    log("=" * 80)
    
    # Sort by total return
    sorted_results = sorted(all_results, key=lambda x: x["total_return_pct"], reverse=True)
    
    log(f"\n{'Label':<50} {'Trades':>7} {'WR%':>6} {'Return%':>9} {'PF':>6} {'Sharpe':>7}")
    log("-" * 90)
    for r in sorted_results:
        log(f"{r['label']:<50} {r['total_trades']:>7} {r['win_rate']:>6} "
            f"{r['total_return_pct']:>9} {r['profit_factor']:>6} {r['sharpe']:>7}")
    
    # ── LEVERAGE TEST for winners >15% ──────────────────
    log("\n" + "=" * 80)
    log("LEVERAGE TEST — Strategies with >15% return at 1x")
    log("=" * 80)
    
    winners = [r for r in sorted_results if r["total_return_pct"] > 15]
    
    if not winners:
        # Test top 5 if none >15%
        log("\nNo strategies exceeded 15% return. Testing top 5 performers with leverage:")
        winners = sorted_results[:5]
    
    leverage_results = []
    for r in winners:
        label_base = r["label"]
        
        # Reconstruct trades for leverage test
        parts = label_base.split(" | ")
        strat = parts[0].strip()
        symbol = parts[1].strip()
        tf = parts[2].strip()
        
        key = f"{symbol}_{tf}"
        if key not in data_cache:
            continue
        
        df = data_cache[key].copy()
        
        if "BB_Squeeze" in strat:
            sl_str = parts[3].strip()
            sl_val = float(sl_str.replace("SL=", "").replace("%", "")) / 100
            df = add_bollinger_bands(df)
            tf_min = TIMEFRAMES[tf]
            base_trades = backtest_bb_squeeze(df, sl_val, tf_min, symbol)
        else:
            df = add_ichimoku(df)
            base_trades = backtest_ichimoku(df, symbol)
        
        for lev in [1, 2, 3]:
            levered_trades = apply_leverage(base_trades, lev)
            lev_label = f"{label_base} | {lev}x"
            m = compute_metrics(levered_trades, lev_label)
            leverage_results.append(m)
            
            log(f"\n{lev_label}")
            log(f"  Return: {m['total_return_pct']}% | WR: {m['win_rate']}% | "
                f"PF: {m['profit_factor']} | Max Loss: {m['max_loss_pct']}%")
    
    # ── FINAL RANKINGS ──────────────────────────────────
    log("\n" + "=" * 80)
    log("FINAL RANKINGS — ALL LEVERAGE CONFIGS")
    log("=" * 80)
    
    all_lev = sorted(leverage_results, key=lambda x: x["total_return_pct"], reverse=True)
    log(f"\n{'Rank':>4} {'Label':<60} {'Return%':>9} {'MaxLoss%':>10} {'PF':>6}")
    log("-" * 95)
    for i, r in enumerate(all_lev, 1):
        log(f"{i:>4} {r['label']:<60} {r['total_return_pct']:>9} "
            f"{r['max_loss_pct']:>10} {r['profit_factor']:>6}")
    
    # ── RECOMMENDATIONS ─────────────────────────────────
    log("\n" + "=" * 80)
    log("RECOMMENDATIONS")
    log("=" * 80)
    
    # Best BB Squeeze config
    bb_results = [r for r in sorted_results if "BB_Squeeze" in r["label"]]
    if bb_results:
        best_bb = bb_results[0]
        log(f"\nBest BB Squeeze: {best_bb['label']}")
        log(f"  Return: {best_bb['total_return_pct']}% | WR: {best_bb['win_rate']}% | "
            f"PF: {best_bb['profit_factor']} | Trades: {best_bb['total_trades']}")
    
    # Best Ichimoku config
    ichi_results = [r for r in sorted_results if "Ichimoku" in r["label"]]
    if ichi_results:
        best_ichi = ichi_results[0]
        log(f"\nBest Ichimoku: {best_ichi['label']}")
        log(f"  Return: {best_ichi['total_return_pct']}% | WR: {best_ichi['win_rate']}% | "
            f"PF: {best_ichi['profit_factor']} | Trades: {best_ichi['total_trades']}")
    
    # Overall best
    if sorted_results:
        best = sorted_results[0]
        log(f"\nOverall Best (1x): {best['label']}")
        log(f"  Return: {best['total_return_pct']}% | WR: {best['win_rate']}% | PF: {best['profit_factor']}")
    
    if all_lev:
        best_lev = all_lev[0]
        log(f"\nOverall Best (with leverage): {best_lev['label']}")
        log(f"  Return: {best_lev['total_return_pct']}% | Max Loss: {best_lev['max_loss_pct']}%")
    
    # ── SAVE ────────────────────────────────────────────
    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(output_lines))
    
    log(f"\n✅ Results saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
