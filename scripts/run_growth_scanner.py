#!/usr/bin/env python3
"""
Growth Scanner → Lighthouse Watchlist Bridge
=============================================
Runs Luna's growth scanner and POSTs results to the Lighthouse /watchlist API.
Designed to be called by a daily cron job at 08:00 UTC.

Usage:
    python run_growth_scanner.py                    # default 15 coins
    python run_growth_scanner.py SOL ETH DOGE       # specific coins
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ── Config ────────────────────────────────────────────────────────────────────

LIGHTHOUSE_URL = os.getenv("LIGHTHOUSE_URL", "http://localhost:8420")
SCANNER_PATH = "/home/yaraclawd/.openclaw/workspace-luna/files/strategy_lab/growth_scanner.py"
RESULTS_FILE = "/tmp/growth_scanner_latest.json"

# Default coins — covers all major candidates + our active bots
DEFAULT_COINS = [
    "SOL/USDT", "FTM/USDT", "DOGE/USDT", "ETH/USDT", "BTC/USDT",
    "MATIC/USDT", "NEAR/USDT", "RUNE/USDT", "AVAX/USDT", "LINK/USDT",
    "ADA/USDT", "SUI/USDT", "PEPE/USDT", "ONDO/USDT", "BNB/USDT",
]


# ── Scanner Integration ──────────────────────────────────────────────────────

def run_scanner(coins):
    """Import and run Luna's growth scanner, return results."""
    # Add scanner directory to path
    scanner_dir = os.path.dirname(SCANNER_PATH)
    if scanner_dir not in sys.path:
        sys.path.insert(0, scanner_dir)

    from growth_scanner import scan_multiple

    print(f"[{datetime.now(timezone.utc).isoformat()}] Scanning {len(coins)} coins...")
    start = time.time()
    results = scan_multiple(coins)
    elapsed = time.time() - start
    print(f"Scan complete in {elapsed:.1f}s — {len(results)} results")

    return results


def results_to_payload(results):
    """Convert scanner results to Lighthouse API payload."""
    coins = []
    for r in results:
        if "error" in r:
            print(f"  ⚠️  {r['symbol']}: {r['error']}")
            continue

        details = r.get("details", {})

        # Determine trend from ROC and score
        roc_30 = details.get("current_roc30")
        score = r.get("growth_score", 0)
        if roc_30 is not None and roc_30 > 5 and score >= 60:
            trend = "bullish"
        elif roc_30 is not None and roc_30 < -5:
            trend = "bearish"
        else:
            trend = "neutral"

        coins.append({
            "symbol": r["symbol"],
            "score": r.get("growth_score", 0),
            "price": details.get("current_price"),
            "roc_30": roc_30,
            "roc_asymmetry": r.get("roc_asymmetry"),
            "price_multiple": r.get("price_multiple"),
            "volume_24h": details.get("avg_daily_volume"),
            "trend": trend,
            "details": {
                "pct_roc_above_50": r.get("pct_time_roc_above_50"),
                "max_roc": details.get("max_roc30"),
                "data_days": details.get("total_days"),
                "growth_phases": details.get("growth_phase_count"),
            },
        })

    return {
        "scanner_version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coins": coins,
    }


def post_to_lighthouse(payload):
    """POST scan results to Lighthouse watchlist API."""
    url = f"{LIGHTHOUSE_URL}/watchlist"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        print(f"✅ Posted to Lighthouse: {result['coins_received']} coins, "
              f"{result['flagged']} flagged ({', '.join(result.get('flagged_coins', []))})")
        return result
    except requests.RequestException as e:
        print(f"❌ Failed to POST to Lighthouse: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        coins = [f"{c.upper()}/USDT" if "/" not in c else c.upper() for c in sys.argv[1:]]
    else:
        coins = DEFAULT_COINS

    # Run scanner
    results = run_scanner(coins)

    # Save raw results locally
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Raw results saved to {RESULTS_FILE}")

    # Convert and POST
    payload = results_to_payload(results)
    post_to_lighthouse(payload)

    # Summary
    scored = [c for c in payload["coins"] if c["score"] >= 70]
    if scored:
        print(f"\n🚨 FLAGGED COINS (score >= 70):")
        for c in sorted(scored, key=lambda x: x["score"], reverse=True):
            print(f"  {c['symbol']:12s} score={c['score']:5.1f} ROC30={c.get('roc_30','N/A')} trend={c['trend']}")


if __name__ == "__main__":
    main()
