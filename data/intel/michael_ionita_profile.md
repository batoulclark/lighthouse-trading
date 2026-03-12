# Michael Ionita — Competitive Intelligence Profile

_Last updated: 2026-03-12_

---

## Person
- **Name:** Michael Ionita
- **Title:** Former VP of Engineering at DappRadar (17yr dev experience)
- **YouTube:** @michaelionita (personal), @signum-app (product)
- **Twitter/X:** @mizi
- **LinkedIn:** /in/michaelionita
- **Website:** autotrading.vip
- **Product:** signum.money
- **Telegram Group:** https://t.me/+DvtY0IPAfFQyMWI0

---

## Business Model
1. **AutoTrading Masterclass** — paid course on Teachable (autotrading.vip), limited to 1000 members
2. **Signum.money** — SaaS TradingView-to-exchange bridge, subscription model
3. **Exchange referrals** — Bybit, Binance, OKX, KuCoin, Bitget, Gate, MEXC, Crypto.com, Hyperliquid
4. **YouTube** — content marketing funnel → course/Signum sales
5. **AI Backtesting Engine** — digital product, sold via autotrading.vip

---

## Strategy Catalog (from Signum code analysis)

### Public Strategies
| Strategy | Type | Access |
|----------|------|--------|
| Gaussian Channel | Trend following | Signum subscribers |
| Ichimoku EMA 4H | Ichimoku + EMA | Signum subscribers |
| Hull Suite | Hull MA | Free / everyone |
| Ichimoku TK Cross | Tenkan/Kijun cross | Free / everyone |

### Hidden/Premium Strategies (AutoTrading members only)
| Strategy | Type | Notes |
|----------|------|-------|
| **GAUSSIAN_CHANNEL_PRO** | Enhanced Gaussian | HIDDEN from UI — filtered out in code |
| **BOT2_FAST_REENTRY_CHOP** | Chop market variant | For sideways markets |
| **BOT2_SLOW_STOCKS** | Stock-specific | Adapted for equities |
| **MONEY_LINE** | Unknown | No public information |

### Strategy Families (from videos)
- **Bot1 (Momentum):** Entries on band cross, quick exits. Good for volatile assets.
- **Bot2Slow (Trend):** Trades seldom, avoids fakeouts. High win-rate.
- **Bot2FastReEntry (Hybrid):** Best of Bot1 + Bot2Slow. Low Max DD on altcoins. ← "The model"

---

## Technical Infrastructure

### Signum.money Architecture
- **Backend:** api.signum.money (authenticated REST API)
- **Frontend:** Vue.js SPA (app.signum.money)
- **Auth:** Email/password
- **Payments:** Stripe (4 products: Basic/AutoTrading × Monthly/Yearly)
- **Analytics:** PostHog
- **Chatbot:** Wonderchat
- **Feedback:** Canny

### API Endpoints (discovered)
- `/authenticate` — login
- `/bots` — CRUD bot management
- `/exchanges` — supported exchanges
- `/exchange/pairs/{id}` — trading pairs
- `/exchange/assets/{id}` — balances
- `/exchange/credentials/{id}` — API keys
- `/signals` — trading signals
- `/strategies` — strategy catalog
- `/strategies/add` — grant TV access
- `/users/me` — user profile
- `/auditlog/bot/{id}` — trade logs

### Signal Format (Schema v2)
```json
{
  "bot_id": "<secret>",
  "ticker": "BTCUSDT",
  "action": "buy|sell",
  "order_size": "100%",
  "position_size": "1|0|-1",
  "timestamp": "ISO8601",
  "schema": "2"
}
```

### Supported Exchanges
Binance, Bybit, OKX, Kraken, KuCoin, Coinbase, Bitget, Gate, MEXC, Hyperliquid

### Hyperliquid Integration
- Uses Public Address + API Wallet Address
- USDC collateral for futures
- Spot requires Perps ⇆ Spot transfer

---

## AI Strategy Building (from YouTube analysis)

### Video: "Claude Opus 4.6 Backtesting Engine" (Zepx8mARre0)
- Uses Claude Opus 4.6 Desktop App
- Workflow: Export TV CSV → Feed to Claude → Generate PineScript → Backtest → Iterate
- Has an "AI Backtesting Engine" product (paid)
- Strategy scoring/ranking system
- Overfitting awareness

### Video: "Claude AI Auto Build/Improve Strategies" (77ikjQjdGFg)
- Automated strategy improvement loop
- Claude reads backtest results → tweaks → retests
- Strategy scoring/ranking criteria
- Uses TV chart data export

### Video: "Can AI Create Strategy by ITSELF" (HzA8o5fg5T8)
- Convert indicators to strategies
- Improve existing strategies with AI
- Create new strategies from scratch
- Re-create closed-source indicators (reverse engineering)

### Video: "GPT 5.2 for TV Strategies" (qbyQ8322m-M)
- KPI-driven strategy performance evaluation
- "BEST Overall Crypto Strategy" ranking
- Claims a "plot twist" result

---

## Our Competitive Position

### Where We're Ahead
- ✅ Long+Short strategies (he's long-only publicly)
- ✅ Automated parameter optimization (37K+ configs tested)
- ✅ Kill switch + Emergency stop-loss (Signum has none)
- ✅ Our best backtest: +4,250% / 18.7% MDD vs his ~2,700% / 27% MDD
- ✅ Full code ownership (no third-party dependency)
- ✅ Funding rate filter (unique to our approach)
- ✅ Time filters (session-based, day-of-week)

### Where He's Ahead
- 📢 Polished marketing + YouTube presence
- 👥 Community (1000-member masterclass)
- 💰 Revenue streams (course + SaaS + referrals)
- 🔒 Hidden PRO strategies we can't compare against
- 🎓 Established brand (autotrading.vip)

### Unknown — Need More Data
- GAUSSIAN_CHANNEL_PRO exact modifications
- BOT2_FAST_REENTRY_CHOP filter logic
- MONEY_LINE strategy entirely
- His strategy scoring formula
- His actual live trading performance (vs backtest claims)

---

## Monitoring Schedule
- **Weekly:** Run `scripts/monitor_michael.py` every Sunday
- **Track:** New videos, strategy mentions, Signum updates
- **Intel stored:** `data/intel/` directory

---

_Luna 📊 — Competitive Intelligence, Mar 12, 2026_
