# 🔦 Lighthouse Trading

> **Personal Automated Trading Platform** — Bridge TradingView signals to exchange execution.

Built by **Luna** (Financial Analyst) for **Jean Sayah**.

---

## Overview

Lighthouse Trading receives webhook alerts from TradingView and automatically executes trades on Hyperliquid (and Binance). It includes a layered safety system, Telegram notifications, and a full REST API for bot management.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      TradingView Alert                          │
│          (Signum schema v2 JSON via webhook)                    │
└────────────────────────┬────────────────────────────────────────┘
                         │ POST /webhook/{bot_id}
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Server :8420                         │
│                                                                 │
│  ┌──────────────┐    ┌─────────────────────┐                   │
│  │  IP Allowlist│    │  SignalProcessor     │                   │
│  │  Rate Limit  │───▶│  - Schema v2 check  │                   │
│  │  Dedup (60s) │    │  - Bot auth         │                   │
│  └──────────────┘    │  - Ticker match     │                   │
│                      │  - Rate limit       │                   │
│                      │  - Deduplication    │                   │
│                      └──────────┬──────────┘                   │
│                                 │                               │
│                      ┌──────────▼──────────┐                   │
│                      │   Safety Gates      │                   │
│                      │  ┌───────────────┐  │                   │
│                      │  │  Kill Switch  │  │                   │
│                      │  │  (file-based) │  │                   │
│                      │  └───────────────┘  │                   │
│                      └──────────┬──────────┘                   │
│                                 │                               │
│                      ┌──────────▼──────────┐                   │
│                      │   OrderExecutor     │                   │
│                      │  - Set leverage     │                   │
│                      │  - Calc size        │                   │
│                      │  - Route to exchange│                   │
│                      └──────────┬──────────┘                   │
└─────────────────────────────────┼───────────────────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐
   │   Hyperliquid    │  │     Binance      │  │  Trade Log   │
   │   Connector      │  │     Connector    │  │  + Backup    │
   └──────────────────┘  └──────────────────┘  └──────────────┘
              │
              ▼
   ┌──────────────────┐
   │  Telegram Alert  │
   └──────────────────┘

Background: EmergencyStopLoss monitor (every 60s)
  → checks unrealized PnL vs equity
  → WARN at 15% / CLOSE at 20% / CLOSE+KILL at 30%
```

---

## Signal Flow

```
TV Alert → POST /webhook/{bot_id}
         → Validate IP allowlist
         → Parse Signum v2 JSON
         → Authenticate bot_id (webhook_secret)
         → Verify ticker matches bot pair
         → Dedup check (60s window)
         → Rate limit (5s per bot)
         → Check Kill Switch
         → Set leverage on exchange
         → Resolve order size (% of balance or fixed)
         → Execute market order
         → Log trade to data/trades.json
         → Telegram alert
         → Return JSON response
```

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/jean-sayah/lighthouse-trading.git
cd lighthouse-trading
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — add your HL_PRIVATE_KEY, HL_ACCOUNT_ADDRESS, TELEGRAM_BOT_TOKEN, etc.
nano .env
```

### 3. Create your first bot

```bash
python scripts/create_bot.py create \
  --name "BTC Trend" \
  --exchange hyperliquid \
  --pair BTCUSDT \
  --leverage 5
```

Output includes your **Webhook URL** and a sample **TradingView alert JSON**.

### 4. Start the server

```bash
bash scripts/start.sh
# or directly:
uvicorn main:app --host 0.0.0.0 --port 8420
```

### 5. Check health

```bash
curl http://localhost:8420/health
```

---

## TradingView Setup

1. Open any chart → Alerts → Create Alert
2. Set condition, then in **Notifications** check **Webhook URL**
3. Enter: `http://YOUR_SERVER_IP:8420/webhook/YOUR_BOT_UUID`
4. In **Message**, paste the Signum v2 JSON:

```json
{
  "bot_id": "YOUR_WEBHOOK_SECRET",
  "ticker": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "order_size": "100%",
  "position_size": "{{strategy.position_size}}",
  "timestamp": "{{timenow}}",
  "schema": "2"
}
```

> **Note:** `bot_id` in the JSON is the **webhook_secret** (not the bot UUID).

---

## API Reference

### Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Server liveness check |

### Webhooks

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/webhook/{bot_id}` | Secret in payload | Receive TV signal |

### Bot Management

All write endpoints require `X-API-Key: YOUR_LIGHTHOUSE_API_KEY` header.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/bots` | None | List all bots |
| GET | `/bots/{id}` | None | Get bot by UUID |
| POST | `/bots` | API key | Create bot |
| PATCH | `/bots/{id}` | API key | Update bot |
| DELETE | `/bots/{id}` | API key | Delete bot |
| POST | `/bots/kill-switch/activate` | API key | Halt all trading |
| POST | `/bots/kill-switch/deactivate` | API key | Resume trading |

### Create Bot (POST /bots)

```json
{
  "name": "ETH Scalper",
  "exchange": "hyperliquid",
  "pair": "ETHUSDT",
  "leverage": 3,
  "webhook_secret": "optional-custom-secret"
}
```

---

## CLI Reference

```bash
# Create a bot
python scripts/create_bot.py create --name "BTC" --exchange hyperliquid --pair BTCUSDT --leverage 5

# List all bots
python scripts/create_bot.py list

# Enable / disable
python scripts/create_bot.py enable <bot-id>
python scripts/create_bot.py disable <bot-id>

# Delete
python scripts/create_bot.py delete <bot-id>
```

---

## Safety System

### Kill Switch

Create a file named `KILL_SWITCH` in the project root to immediately halt **all** trade execution. Lighthouse refuses every order while this file exists and sends a Telegram alert.

```bash
# Halt trading
touch KILL_SWITCH

# Resume trading
rm KILL_SWITCH

# Via API
curl -X POST http://localhost:8420/bots/kill-switch/activate \
  -H "X-API-Key: YOUR_API_KEY"
```

### Emergency Stop-Loss (ESL)

Runs every 60 seconds, monitoring unrealized PnL across all open positions:

| Threshold | Action |
|-----------|--------|
| 15% (warn) | Telegram warning — no position changes |
| 20% (critical) | Force-close all open positions |
| 30% (catastrophic) | Force-close all positions + activate Kill Switch |

Configure thresholds via `.env`:

```
ESL_WARN_PCT=15
ESL_CRITICAL_PCT=20
ESL_CATASTROPHIC_PCT=30
```

### State Backup

Every startup and shutdown creates a timestamped snapshot of bots + trades to two independent directories. Old backups are rotated (default: keep last 30).

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LIGHTHOUSE_HOST` | `0.0.0.0` | Bind address |
| `LIGHTHOUSE_PORT` | `8420` | Listen port |
| `LIGHTHOUSE_API_KEY` | — | Secret for management API |
| `HL_PRIVATE_KEY` | — | Hyperliquid wallet private key |
| `HL_ACCOUNT_ADDRESS` | — | Hyperliquid public address |
| `HL_TESTNET` | `true` | Use testnet (set `false` for mainnet) |
| `BINANCE_API_KEY` | — | Binance API key |
| `BINANCE_API_SECRET` | — | Binance API secret |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `7422563444` | Telegram chat/user ID |
| `ESL_WARN_PCT` | `15` | ESL warning threshold % |
| `ESL_CRITICAL_PCT` | `20` | ESL force-close threshold % |
| `ESL_CATASTROPHIC_PCT` | `30` | ESL kill-switch threshold % |
| `BACKUP_DIR_1` | `data/backups` | Primary backup directory |
| `BACKUP_DIR_2` | `~/lighthouse-backups` | Secondary backup directory |
| `ALLOWED_IPS` | TradingView IPs | Comma-separated IP allowlist |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
pytest tests/ -v
```

---

## Project Structure

```
lighthouse-trading/
├── main.py                      # FastAPI entry point + lifespan
├── config.py                    # All settings from .env
├── .env.example                 # Environment template
├── requirements.txt
├── app/
│   ├── api/
│   │   ├── webhooks.py          # POST /webhook/{bot_id}
│   │   ├── health.py            # GET /health
│   │   └── bots.py              # Bot CRUD + kill switch
│   ├── exchanges/
│   │   ├── base.py              # Abstract exchange interface
│   │   ├── hyperliquid.py       # Hyperliquid SDK connector
│   │   └── binance_exchange.py  # python-binance connector
│   ├── models/
│   │   ├── bot.py               # Bot dataclass + file store
│   │   ├── signal.py            # Signum v2 signal model
│   │   └── trade.py             # Trade log dataclass
│   ├── safety/
│   │   ├── kill_switch.py       # File-based kill switch
│   │   ├── emergency_sl.py      # Background ESL monitor
│   │   └── state_backup.py      # Dual-location state backup
│   ├── notifications/
│   │   └── telegram.py          # Telegram Bot API notifier
│   └── services/
│       ├── signal_processor.py  # Validate + route signals
│       └── order_executor.py    # Execute orders + log
├── data/
│   ├── bots.json                # Bot configurations
│   └── trades.json              # Trade log (append-only)
├── scripts/
│   ├── start.sh
│   ├── stop.sh
│   └── create_bot.py            # CLI bot manager
└── tests/
    ├── test_webhook.py
    ├── test_signal_processor.py
    ├── test_safety.py
    └── test_order_executor.py
```

---

## Security Notes

- **Never commit `.env`** — it contains private keys.
- The `webhook_secret` in the TradingView alert authenticates the signal source.
- All management API endpoints require `X-API-Key`.
- IP allowlist restricts webhooks to TradingView's published IP ranges by default.
- `HL_TESTNET=true` by default — **explicitly set to `false` for live trading**.

---

## License

MIT © Jean Sayah
