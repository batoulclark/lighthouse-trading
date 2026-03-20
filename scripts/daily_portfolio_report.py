#!/usr/bin/env python3
"""
Daily Portfolio Report — Sends Jean Sarwa + IBKR portfolio values at market close.
Runs via cron at 21:30 UTC daily (after US market close at 20:00 UTC).

Uses live closing prices from web search.
"""
import json
import os
import subprocess
import sys
import urllib.request
import re
from datetime import datetime, timezone

CHAT_ID = "7422563444"

# Sarwa holdings (confirmed by Jean Feb 26)
SARWA = {
    "VTI":  529.9477,
    "IEFA": 1876.8665,
    "VWO":  1036.7889,
    "IBIT": 476.6684,
    "VNQ":  220.8295,
    "BNDX": 278.8325,
    "BND":  181.7378,
}
SARWA_CASH = 4310.0

# IBKR holdings (confirmed Mar 2 screenshot)
IBKR = {
    "ALB":   3,
    "AMZN": 12,
    "AVUV": 17.2003,
    "CRSP":  3.9,
    "GOOG":  7,
    "LLY":   2,
    "NVDA": 12,
    "QQQM": 15.5231,
    "SOFI": 60,
    "URA":  21,
    "VRT":   4,
}
IBKR_CASH = 176.41  # AED 66.49 + USD 158.31 (updated Mar 16 screenshot)


def fetch_price(ticker):
    """Fetch last traded price from Yahoo Finance (regularMarketPrice — matches broker)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        meta = data["chart"]["result"][0]["meta"]
        price = round(meta.get("regularMarketPrice", 0), 2)
        if price > 0:
            return price
    except Exception as e:
        print(f"  Failed to fetch {ticker}: {e}")
    return None


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[{ts}] Daily portfolio report starting")

    # Fetch all prices
    all_tickers = list(SARWA.keys()) + list(IBKR.keys())
    prices = {}
    for ticker in all_tickers:
        if ticker not in prices:
            p = fetch_price(ticker)
            if p:
                prices[ticker] = p
                print(f"  {ticker}: ${p}")
            else:
                print(f"  {ticker}: FAILED")

    # Calculate Sarwa
    sarwa_lines = []
    sarwa_total = SARWA_CASH
    for ticker, shares in SARWA.items():
        if ticker in prices:
            val = shares * prices[ticker]
            sarwa_total += val
            sarwa_lines.append(f"  {ticker}: {shares:.1f} × ${prices[ticker]:,.2f} = ${val:,.0f}")
        else:
            sarwa_lines.append(f"  {ticker}: {shares:.1f} × ⚠️ price unavailable")

    # Calculate IBKR
    ibkr_lines = []
    ibkr_total = IBKR_CASH
    for ticker, shares in IBKR.items():
        if ticker in prices:
            val = shares * prices[ticker]
            ibkr_total += val
            change = ""
            ibkr_lines.append(f"  {ticker}: {shares:g} × ${prices[ticker]:,.2f} = ${val:,.0f}")
        else:
            ibkr_lines.append(f"  {ticker}: {shares:g} × ⚠️ price unavailable")

    combined = sarwa_total + ibkr_total

    msg = (
        f"📊 Portfolio Update — {today}\n"
        f"Prices: latest closing\n\n"
        f"💼 Sarwa: ${sarwa_total:,.0f}\n"
        + "\n".join(sarwa_lines) +
        f"\n  Cash: ${SARWA_CASH:,.0f}\n\n"
        f"📈 IBKR: ${ibkr_total:,.0f}\n"
        + "\n".join(ibkr_lines) +
        f"\n  Cash: ${IBKR_CASH:,.0f}\n\n"
        f"💰 Combined: ${combined:,.0f}"
    )

    # Send via openclaw
    result = subprocess.run(
        ["/home/yaraclawd/.npm-global/bin/openclaw", "message", "send",
         "--channel", "telegram", "--account", "luna",
         "--target", CHAT_ID,
         "-m", msg],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode == 0:
        print(f"Sent to Jean. Sarwa: ${sarwa_total:,.0f} | IBKR: ${ibkr_total:,.0f} | Combined: ${combined:,.0f}")
    else:
        print(f"Send failed: {result.stderr}")


if __name__ == "__main__":
    main()
