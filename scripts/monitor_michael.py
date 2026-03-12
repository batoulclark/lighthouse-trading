#!/usr/bin/env python3
"""
Weekly monitor for Michael Ionita's YouTube channels and public content.
Tracks new videos, extracts strategy insights, and logs everything.

Usage: python3 scripts/monitor_michael.py
Cron: Run weekly (Sundays at 08:00 UTC)
"""

import urllib.request
import json
import re
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "intel"
DATA_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cookie': 'CONSENT=YES+cb.20210328-17-p0.en+FX+999'
}

CHANNELS = {
    "@michaelionita": "Michael Ionita (Personal)",
    "@signum-app": "Signum App (Product)",
}

KNOWN_VIDEOS_FILE = DATA_DIR / "known_videos.json"
INTEL_LOG_FILE = DATA_DIR / "intel_log.json"


def fetch(url: str) -> str:
    """Fetch URL and return text content."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  [ERROR] {url}: {e}")
        return ""


def load_known_videos() -> dict:
    """Load previously seen videos."""
    if KNOWN_VIDEOS_FILE.exists():
        return json.loads(KNOWN_VIDEOS_FILE.read_text())
    return {}


def save_known_videos(videos: dict):
    """Save seen videos."""
    KNOWN_VIDEOS_FILE.write_text(json.dumps(videos, indent=2))


def log_intel(entry: dict):
    """Append to intel log."""
    log = []
    if INTEL_LOG_FILE.exists():
        log = json.loads(INTEL_LOG_FILE.read_text())
    log.append(entry)
    INTEL_LOG_FILE.write_text(json.dumps(log, indent=2))


def scrape_channel(channel_handle: str) -> list:
    """Scrape all visible videos from a YouTube channel."""
    url = f"https://www.youtube.com/{channel_handle}/videos"
    html = fetch(url)
    if not html:
        return []

    video_ids = re.findall(r'"videoId":"([^"]+)"', html)
    titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}', html)

    seen = set()
    videos = []
    for i, vid in enumerate(video_ids):
        if vid not in seen and i < len(titles):
            seen.add(vid)
            videos.append({"id": vid, "title": titles[i]})

    return videos


def get_video_details(video_id: str) -> dict:
    """Get detailed info for a video."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    html = fetch(url)
    if not html:
        return {}

    details = {"id": video_id, "url": f"https://youtube.com/watch?v={video_id}"}

    # Description
    desc = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', html)
    if desc:
        details["description"] = desc.group(1).replace('\\n', '\n').replace('\\u0026', '&')

    # Chapters
    chapters = re.findall(
        r'"title":\{"simpleText":"([^"]+)"\},"timeRangeStartMillis":(\d+)', html
    )
    if chapters:
        details["chapters"] = [
            {"time": f"{int(ms)//60000}:{(int(ms)//1000)%60:02d}", "title": title}
            for title, ms in chapters
        ]

    # Publish date
    pub = re.search(r'"publishDate":"([^"]*)"', html)
    if pub:
        details["published"] = pub.group(1)

    # Duration
    dur = re.search(r'"lengthSeconds":"(\d+)"', html)
    if dur:
        secs = int(dur.group(1))
        details["duration"] = f"{secs//60}:{secs%60:02d}"

    # Strategy keywords
    strategy_keywords = [
        'gaussian', 'macd', 'rsi', 'ema', 'ichimoku', 'bot1', 'bot2',
        'momentum', 'trend', 'reentry', 'money line', 'hull',
        'backtest', 'optimize', 'parameter', 'overfitting',
        'signum', 'webhook', 'automate', 'pine', 'tradingview',
        'claude', 'opus', 'gpt', 'ai', 'chatgpt',
    ]
    found_keywords = [kw for kw in strategy_keywords if kw in html.lower()]
    details["keywords"] = found_keywords

    return details


def check_signum_updates():
    """Check for changes on signum.money."""
    html = fetch("https://signum.money")
    if not html:
        return None

    # Check for new exchange mentions
    exchanges = set()
    for ex in ['binance', 'bybit', 'okx', 'kraken', 'kucoin', 'coinbase',
               'bitget', 'hyperliquid', 'deribit', 'gate', 'mexc']:
        if ex in html.lower():
            exchanges.add(ex)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "exchanges": sorted(exchanges),
        "page_size": len(html),
    }


def main():
    print(f"{'='*60}")
    print(f"MICHAEL IONITA WEEKLY MONITOR — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    known = load_known_videos()
    new_videos = []

    for handle, channel_name in CHANNELS.items():
        print(f"\n📺 Scanning {channel_name} ({handle})...")
        videos = scrape_channel(handle)
        print(f"   Found {len(videos)} videos")

        for video in videos:
            vid_id = video["id"]
            if vid_id not in known:
                print(f"\n   🆕 NEW VIDEO: {video['title']}")
                details = get_video_details(vid_id)
                details["channel"] = channel_name
                details["first_seen"] = datetime.utcnow().isoformat()

                # Log it
                log_intel({
                    "type": "new_video",
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": details,
                })

                known[vid_id] = {
                    "title": video["title"],
                    "channel": channel_name,
                    "first_seen": datetime.utcnow().isoformat(),
                }
                new_videos.append(details)

                # Print details
                if details.get("chapters"):
                    print(f"   Chapters:")
                    for ch in details["chapters"]:
                        print(f"     {ch['time']} - {ch['title']}")
                if details.get("keywords"):
                    print(f"   Keywords: {', '.join(details['keywords'])}")

    # Check Signum updates
    print(f"\n🔌 Checking signum.money...")
    signum = check_signum_updates()
    if signum:
        print(f"   Exchanges: {', '.join(signum['exchanges'])}")
        log_intel({
            "type": "signum_check",
            "timestamp": datetime.utcnow().isoformat(),
            "data": signum,
        })

    # Save state
    save_known_videos(known)

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(new_videos)} new videos found")
    if new_videos:
        for v in new_videos:
            print(f"  🆕 {v.get('channel', '?')}: {known.get(v['id'], {}).get('title', '?')}")
            print(f"     {v.get('url', '')}")
    else:
        print("  No new content this week.")
    print(f"{'='*60}")

    return new_videos


if __name__ == "__main__":
    new = main()
