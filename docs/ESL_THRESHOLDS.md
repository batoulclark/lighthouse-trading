# Emergency Stop-Loss Threshold Rationale
_Written: 2026-03-16 by Luna_

## Current Thresholds
| Level | % Drawdown | Action |
|-------|-----------|--------|
| WARNING | 15% | Telegram alert to Jean |
| CRITICAL | 20% | Force close all positions |
| CATASTROPHIC | 30% | Force close + activate kill switch |

## Rationale

### Why 15% for WARNING
- Strategy MDD in backtest: 17-19% (v6/v7)
- A 15% live drawdown means we're approaching the historical maximum
- Jean should know early enough to intervene if he wants
- At $1,000 capital: $150 loss triggers warning

### Why 20% for CRITICAL
- Exceeds historical MDD by ~1-3pp
- Indicates something unusual: market structure break, API error, bad fill
- Automatic position close limits further damage
- At $1,000 capital: $200 loss triggers force close

### Why 30% for CATASTROPHIC
- Well above any historical MDD
- At this point, either the strategy has broken down or there's a system error
- Kill switch prevents any new trades until Jean manually reviews
- At $1,000 capital: $300 loss triggers full shutdown

## Notes
- These thresholds are for $1,000 initial capital
- Should be revisited if capital grows beyond $10,000
- BTC daily strategy has lower trade frequency — drawdowns develop slowly (days, not hours)
- Kill switch must be manually deactivated by Jean via API or file deletion
