#!/usr/bin/env python3
"""
Lighthouse Trading — CLI tool to create a new bot.

Usage
-----
  python scripts/create_bot.py \\
    --name "BTC Trend" \\
    --exchange hyperliquid \\
    --pair BTCUSDT \\
    --leverage 5

  python scripts/create_bot.py --list
  python scripts/create_bot.py --delete <bot-id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running from repo root or scripts/ directory
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.models.bot import Bot, BotStore
from config import settings


def _store() -> BotStore:
    return BotStore(settings.bots_file)


def cmd_create(args: argparse.Namespace) -> None:
    store = _store()
    bot = Bot.create(
        name=args.name,
        exchange=args.exchange,
        pair=args.pair,
        leverage=args.leverage,
        webhook_secret=args.secret or None,
    )
    store.add(bot)
    print(f"\n✅ Bot created successfully!")
    print(f"   ID              : {bot.id}")
    print(f"   Name            : {bot.name}")
    print(f"   Exchange        : {bot.exchange}")
    print(f"   Pair            : {bot.pair}")
    print(f"   Leverage        : {bot.leverage}x")
    print(f"   Webhook secret  : {bot.webhook_secret}")
    print(f"\n📡 TradingView alert JSON:")
    sample = {
        "bot_id": bot.webhook_secret,
        "ticker": bot.pair,
        "action": "buy",
        "order_size": "100%",
        "position_size": "1",
        "timestamp": "{{timenow}}",
        "schema": "2",
    }
    print(json.dumps(sample, indent=2))
    print(f"\n🔗 Webhook URL: http://<your-server>:{settings.port}/webhook/{bot.id}")


def cmd_list(args: argparse.Namespace) -> None:
    store = _store()
    bots = store.all()
    if not bots:
        print("No bots configured.")
        return
    print(f"\n{'ID':<38}  {'Name':<20}  {'Exchange':<12}  {'Pair':<12}  {'Lev':>4}  {'Enabled'}")
    print("-" * 100)
    for b in bots:
        print(
            f"{b.id:<38}  {b.name:<20}  {b.exchange:<12}  {b.pair:<12}  "
            f"{b.leverage:>4}x  {'✅' if b.enabled else '❌'}"
        )


def cmd_delete(args: argparse.Namespace) -> None:
    store = _store()
    if store.delete(args.bot_id):
        print(f"✅ Bot {args.bot_id} deleted.")
    else:
        print(f"❌ Bot {args.bot_id} not found.")
        sys.exit(1)


def cmd_enable(args: argparse.Namespace) -> None:
    store = _store()
    bot = store.get(args.bot_id)
    if bot is None:
        print(f"❌ Bot {args.bot_id} not found.")
        sys.exit(1)
    bot.enabled = True
    store.update(bot)
    print(f"✅ Bot '{bot.name}' enabled.")


def cmd_disable(args: argparse.Namespace) -> None:
    store = _store()
    bot = store.get(args.bot_id)
    if bot is None:
        print(f"❌ Bot {args.bot_id} not found.")
        sys.exit(1)
    bot.enabled = False
    store.update(bot)
    print(f"⏸️  Bot '{bot.name}' disabled.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="create_bot",
        description="Lighthouse Trading — bot management CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Create a new bot")
    p_create.add_argument("--name", required=True, help="Human-readable bot name")
    p_create.add_argument(
        "--exchange",
        required=True,
        choices=["hyperliquid", "binance"],
        help="Exchange to trade on",
    )
    p_create.add_argument("--pair", required=True, help="Trading pair, e.g. BTCUSDT")
    p_create.add_argument("--leverage", type=int, default=1, help="Leverage (default 1)")
    p_create.add_argument("--secret", default=None, help="Custom webhook secret (auto-generated if omitted)")

    # list
    sub.add_parser("list", help="List all bots")

    # delete
    p_del = sub.add_parser("delete", help="Delete a bot by ID")
    p_del.add_argument("bot_id")

    # enable / disable
    p_en = sub.add_parser("enable", help="Enable a bot")
    p_en.add_argument("bot_id")
    p_dis = sub.add_parser("disable", help="Disable a bot")
    p_dis.add_argument("bot_id")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "enable":
        cmd_enable(args)
    elif args.command == "disable":
        cmd_disable(args)
    else:
        # Backwards-compat: direct --name/--exchange/--pair creates a bot
        if hasattr(args, "name") and args.name:
            cmd_create(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
