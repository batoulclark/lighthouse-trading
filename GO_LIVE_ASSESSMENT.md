# Lighthouse Trading — Go-Live Assessment
_Last updated: 2026-03-16 18:46 UTC by Luna_

## Status: NOT READY — Blockers Remain

### ✅ What's Working

| Component | Status | Detail |
|-----------|--------|--------|
| Server | ✅ | Running 48h+ stable, HTTPS with valid TLS cert (expires Jun 10) |
| Webhook pipeline | ✅ | TV → Caddy (HTTPS) → Lighthouse → Paper Exchange |
| IP allowlist | ✅ | Only TV IPs + localhost accepted |
| API key auth | ✅ | Bot management locked behind API key |
| Kill switch | ✅ | File-based, survives reboots |
| Backup signal | ✅ | Runs daily at 00:05 UTC, catches missed TV signals |
| Strategy lab | ✅ | Running every 6h, 13+ rounds complete |
| TLS cert | ✅ | Valid until Jun 10, 2026 |
| Code on GitHub | ✅ | batoulclark/lighthouse-trading |
| Position guard | ✅ | Prevents double-buy on duplicate signals |
| Emergency SL | ✅ | 15% warn, 20% critical, 30% catastrophic |
| State backup | ✅ | 2 locations, 30 rotation, validation |

### 🔴 Blockers — Must Fix Before Real Money

| # | Issue | Risk | Fix | Owner |
|---|-------|------|-----|-------|
| 1 | EXCHANGE_MODE=paper | Can't trade real money | Switch to `live` + add HL keys | Jean + Luna |
| 2 | No HL API keys | Can't connect to Hyperliquid | Jean provides testnet keys first | Jean |
| 3 | No testnet validation | Never tested on real exchange API | 2 weeks on HL testnet | Luna |
| 4 | No auto-restart | Server dies, trades missed | Systemd service (Yara) + watchdog (Luna) | Yara + Luna |
| 5 | TV strategy not validated | Best config not confirmed on TV | Jean pastes PineScript | Jean |
| 6 | Double-buy guard untested live | Only tested on paper | Test on testnet | Luna |
| 7 | No position size limits | Uses 100% equity per trade | Fine for $1K | Luna (if capital grows) |

### ⚠️ Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Server crash with open position | HIGH | Multi-layer restart (systemd + watchdog cron). ESL resumes on restart. |
| TV alert fails | MEDIUM | Backup signal at 00:05 UTC. Max 24h delay for daily strategy. |
| Exchange API error | MEDIUM | Add retry logic (3 attempts, exponential backoff). TODO. |
| Foufi bot token shared | LOW | Yara's Foufi agent uses same token. Need separation. |
| BTC flash crash during signal | LOW | Market orders. Consider limit orders with timeout. |
| Strategy overfitting | MEDIUM | Python/TV ratio ~2.80x. Live returns will differ from backtest. |

### 💰 Capital & Expectations

- Starting capital: $1,000 (Jean confirmed)
- TV backtest return (v6): +1,781% over 7 years
- New best (pending TV validation): est. +2,302% TV
- MDD risk: up to 30% ($300 on $1K)
- Strategy currently FLAT — BTC below SMA200

### 🗓️ Go-Live Timeline

| Week | Action |
|------|--------|
| Now | Jean validates best config on TV. Provides HL testnet keys. |
| Week 1 (Mar 17-23) | HL testnet integration. Test full cycle: webhook → API → fills. |
| Week 2 (Mar 24-30) | Testnet live with real TV signals. Fix bugs. |
| Week 3 (Mar 31-Apr 6) | Buffer week. Verify fills, slippage, error handling. |
| Apr 7-15 | Final checks. Systemd installed. Go-live with $1,000. |

### Server Resilience Plan

1. **Layer 1 — Systemd** (Yara installs): `Restart=on-failure` (NOT `always`), `RestartSec=10`, no Telegram on start/stop
2. **Layer 2 — Watchdog cron** (Luna): Every 5 min check, restart if down, alert ONLY on failed recovery
3. **Layer 3 — Backup signal** (cron): Independent Python script at 00:05 UTC catches any missed daily signal
4. **Layer 4 — Manual check** (Luna): Heartbeat every 30 min verifies server health
5. **Max downtime target**: < 5 minutes (systemd + watchdog combined)

---

_Prepared by Luna 📊 — 2026-03-16_
