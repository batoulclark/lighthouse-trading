"""
Lighthouse Trading - Configuration
Loads all settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


def _get_list(key: str, default: str = "") -> List[str]:
    """Parse a comma-separated env var into a list of strings."""
    raw = os.getenv(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    # Server
    host: str = field(default_factory=lambda: os.getenv("LIGHTHOUSE_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("LIGHTHOUSE_PORT", "8420")))
    api_key: str = field(default_factory=lambda: os.getenv("LIGHTHOUSE_API_KEY", ""))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Hyperliquid
    hl_private_key: str = field(default_factory=lambda: os.getenv("HL_PRIVATE_KEY", ""))
    hl_account_address: str = field(default_factory=lambda: os.getenv("HL_ACCOUNT_ADDRESS", ""))
    hl_testnet: bool = field(
        default_factory=lambda: os.getenv("HL_TESTNET", "true").lower() == "true"
    )

    # Binance
    binance_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    binance_api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "7422563444")
    )

    # Emergency Stop-Loss thresholds (as percentages, positive = loss)
    esl_warn_pct: float = field(
        default_factory=lambda: float(os.getenv("ESL_WARN_PCT", "15"))
    )
    esl_critical_pct: float = field(
        default_factory=lambda: float(os.getenv("ESL_CRITICAL_PCT", "20"))
    )
    esl_catastrophic_pct: float = field(
        default_factory=lambda: float(os.getenv("ESL_CATASTROPHIC_PCT", "30"))
    )

    # Backups
    backup_dir_1: str = field(
        default_factory=lambda: os.getenv("BACKUP_DIR_1", "data/backups")
    )
    backup_dir_2: str = field(
        default_factory=lambda: os.path.expanduser(
            os.getenv("BACKUP_DIR_2", "~/lighthouse-backups")
        )
    )
    backup_rotation: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_ROTATION", "30"))
    )

    # IP Allowlist (TradingView IPs + custom)
    allowed_ips: List[str] = field(
        default_factory=lambda: _get_list(
            "ALLOWED_IPS",
            "52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7",
        )
    )

    # Paths
    bots_file: str = field(
        default_factory=lambda: os.getenv("BOTS_FILE", "data/bots.json")
    )
    trades_file: str = field(
        default_factory=lambda: os.getenv("TRADES_FILE", "data/trades.json")
    )
    kill_switch_file: str = field(
        default_factory=lambda: os.getenv("KILL_SWITCH_FILE", "KILL_SWITCH")
    )


# Global singleton — import this everywhere
settings = Config()
