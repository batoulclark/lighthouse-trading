"""
Lighthouse Trading — Monitoring Service.

Background asyncio task that checks system health every 60 seconds and
sends Telegram alerts for WARNING / CRITICAL conditions.

Checks
------
1. Kill switch status
2. Exchange connectivity
3. Position health  (any position open > 7 days)
4. Drawdown vs peak equity  (alerts at 10%, 15%, 20%)
5. Stale signal check  (no new trades > 24h while enabled bots exist)

Alert history is persisted to data/alerts.json (last 1 000 entries, rotated).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.exchanges.base import BaseExchange
    from app.models.bot import BotStore
    from app.models.trade import TradeLog
    from app.notifications.telegram import TelegramNotifier
    from app.safety.kill_switch import KillSwitch
    from app.services.position_manager import PositionManager

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

_ALERTS_FILE   = "data/alerts.json"
_MAX_ALERTS    = 1_000
_POS_MAX_DAYS  = 7     # warn if a position is open longer than this
_STALE_HOURS   = 24    # warn if no trades for this many hours (bots active)
_DD_WARN_PCT   = 10.0  # % drawdown — WARNING
_DD_HIGH_PCT   = 15.0  # % drawdown — WARNING (elevated)
_DD_CRIT_PCT   = 20.0  # % drawdown — CRITICAL


# ── Alert level constants ─────────────────────────────────────────────────────

class AlertLevel:
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


# ── Service ───────────────────────────────────────────────────────────────────

class MonitorService:
    """
    Background monitoring service.

    Typical wiring in the FastAPI lifespan::

        monitor = MonitorService(
            kill_switch=kill_switch,
            position_manager=position_manager,
            bot_store=bot_store,
            telegram=telegram,
            trade_log=trade_log,
            exchanges=exchanges,
        )
        await monitor.start()
        ...
        await monitor.stop()
    """

    def __init__(
        self,
        kill_switch: "KillSwitch",
        position_manager: "PositionManager",
        bot_store: "BotStore",
        telegram: "TelegramNotifier",
        trade_log: "TradeLog",
        exchanges: Optional[Dict[str, "BaseExchange"]] = None,
        alerts_file: str = _ALERTS_FILE,
        interval_seconds: int = 60,
    ) -> None:
        self.kill_switch      = kill_switch
        self.position_manager = position_manager
        self.bot_store        = bot_store
        self.telegram         = telegram
        self.trade_log        = trade_log
        self.exchanges        = exchanges or {}
        self.alerts_file      = alerts_file
        self.interval         = interval_seconds

        self._running   = False
        self._task: Optional[asyncio.Task] = None
        self._peak_equity: Dict[str, float] = {}  # exchange_name → peak equity

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="monitor_service")
        logger.info("Monitor service started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        """Stop the background monitoring loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Monitor service stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Monitor check error: %s", exc)
            await asyncio.sleep(self.interval)

    # ── Public API ────────────────────────────────────────────────────────────

    async def check_all(self) -> List[Dict[str, Any]]:
        """Run all checks and return the list of alerts generated this cycle."""
        alerts: List[Dict[str, Any]] = []

        alerts.extend(await self._check_kill_switch())
        alerts.extend(await self._check_connectivity())
        alerts.extend(await self._check_position_health())
        alerts.extend(await self._check_drawdown())
        alerts.extend(await self._check_stale_signals())

        for alert in alerts:
            self._persist_alert(alert)
            level = alert.get("level", AlertLevel.INFO)
            if level in (AlertLevel.WARNING, AlertLevel.CRITICAL):
                try:
                    await self.telegram.send(self._format_alert(alert))
                except Exception as exc:
                    logger.error("Monitor telegram send failed: %s", exc)

        return alerts

    def get_alert_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return up to `limit` most-recent alerts, newest first."""
        all_alerts = self._load_alerts()
        return list(reversed(all_alerts[-limit:]))

    # ── Individual checks ─────────────────────────────────────────────────────

    async def _check_kill_switch(self) -> List[Dict[str, Any]]:
        if not self.kill_switch.is_active():
            return []
        reason = self.kill_switch.read_reason()
        return [self._make_alert(
            level=AlertLevel.WARNING,
            check="kill_switch",
            message=f"Kill switch is ACTIVE — trading halted. {reason[:200]}",
        )]

    async def _check_connectivity(self) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []
        for name, exchange in self.exchanges.items():
            try:
                await asyncio.wait_for(exchange.get_equity(), timeout=10.0)
            except asyncio.TimeoutError:
                alerts.append(self._make_alert(
                    level=AlertLevel.CRITICAL,
                    check="connectivity",
                    message=f"Exchange {name!r} timed out (>10s) — possible outage",
                    extra={"exchange": name},
                ))
            except Exception as exc:
                alerts.append(self._make_alert(
                    level=AlertLevel.WARNING,
                    check="connectivity",
                    message=f"Exchange {name!r} connectivity error: {exc}",
                    extra={"exchange": name},
                ))
        return alerts

    async def _check_position_health(self) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []
        try:
            positions = self.position_manager.get_all_positions()
        except Exception as exc:
            logger.debug("Could not load positions for health check: %s", exc)
            return []

        now = datetime.now(timezone.utc)

        for pos in positions:
            try:
                opened_at = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                age       = now - opened_at
                if age.days > _POS_MAX_DAYS:
                    alerts.append(self._make_alert(
                        level=AlertLevel.WARNING,
                        check="position_health",
                        message=(
                            f"Position {pos.symbol} (bot={pos.bot_id}) has been open "
                            f"{age.days}d — exceeds {_POS_MAX_DAYS}d threshold"
                        ),
                        extra={
                            "symbol":   pos.symbol,
                            "bot_id":   pos.bot_id,
                            "age_days": age.days,
                        },
                    ))
            except Exception as exc:
                logger.debug("Position health parse error for %s: %s", pos.symbol, exc)

        return alerts

    async def _check_drawdown(self) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []

        for name, exchange in self.exchanges.items():
            try:
                equity = await asyncio.wait_for(exchange.get_equity(), timeout=10.0)
            except Exception:
                continue  # connectivity already covered above

            if equity <= 0:
                continue

            # Maintain the high-water mark
            peak = self._peak_equity.get(name, equity)
            if equity > peak:
                peak = equity
                self._peak_equity[name] = peak

            drawdown_pct = ((peak - equity) / peak) * 100.0

            if drawdown_pct >= _DD_CRIT_PCT:
                level = AlertLevel.CRITICAL
                emoji = "🔴"
            elif drawdown_pct >= _DD_HIGH_PCT:
                level = AlertLevel.WARNING
                emoji = "🟠"
            elif drawdown_pct >= _DD_WARN_PCT:
                level = AlertLevel.WARNING
                emoji = "⚠️"
            else:
                continue

            alerts.append(self._make_alert(
                level=level,
                check="drawdown",
                message=(
                    f"{emoji} Drawdown {drawdown_pct:.1f}% on {name} "
                    f"(equity=${equity:,.2f}, peak=${peak:,.2f})"
                ),
                extra={
                    "exchange":     name,
                    "drawdown_pct": round(drawdown_pct, 2),
                    "equity":       equity,
                    "peak":         peak,
                },
            ))

        return alerts

    async def _check_stale_signals(self) -> List[Dict[str, Any]]:
        """Warn if no new trades in > 24 h while enabled bots exist."""
        active_bots = [b for b in self.bot_store.all() if b.enabled]
        if not active_bots:
            return []

        try:
            all_trades = self.trade_log.all()
        except Exception:
            return []

        if not all_trades:
            return []

        most_recent: Optional[datetime] = None
        for trade in all_trades:
            ts_str = trade.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if most_recent is None or ts > most_recent:
                    most_recent = ts
            except ValueError:
                pass

        if most_recent is None:
            return []

        now = datetime.now(timezone.utc)
        age = now - most_recent

        if age > timedelta(hours=_STALE_HOURS):
            hours = age.total_seconds() / 3600
            return [self._make_alert(
                level=AlertLevel.WARNING,
                check="stale_signals",
                message=(
                    f"No signals in {hours:.1f}h — "
                    f"{len(active_bots)} bot(s) active. "
                    f"Last trade: {most_recent.strftime('%Y-%m-%d %H:%M UTC')}"
                ),
                extra={
                    "hours_since_last_trade": round(hours, 1),
                    "active_bot_count":       len(active_bots),
                },
            )]

        return []

    # ── Alert helpers ─────────────────────────────────────────────────────────

    def _make_alert(
        self,
        level: str,
        check: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        alert: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     level,
            "check":     check,
            "message":   message,
        }
        if extra:
            alert.update(extra)
        return alert

    def _format_alert(self, alert: Dict[str, Any]) -> str:
        """Format an alert dict into a Telegram-ready Markdown string."""
        level   = alert.get("level", "INFO")
        check   = alert.get("check", "?")
        message = alert.get("message", "")
        ts      = alert.get("timestamp", "")[:19].replace("T", " ")

        icon = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}.get(level, "📢")
        return f"{icon} *[{level}]* `{check}`\n{message}\n_{ts} UTC_"

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_alerts(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.alerts_file):
            return []
        try:
            with open(self.alerts_file) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _persist_alert(self, alert: Dict[str, Any]) -> None:
        alerts = self._load_alerts()
        alerts.append(alert)
        if len(alerts) > _MAX_ALERTS:
            alerts = alerts[-_MAX_ALERTS:]
        dir_path = os.path.dirname(self.alerts_file)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        try:
            with open(self.alerts_file, "w") as f:
                json.dump(alerts, f, indent=2)
        except OSError as exc:
            logger.error("Failed to persist alert: %s", exc)
