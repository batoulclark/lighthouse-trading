"""
Tests for the web dashboard:
- GET / serves the HTML page (app/api/web.py + app/web/dashboard.html)
- GET /monitor/alerts returns alert data
- HTML contains expected structural elements
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import web as web_router_module
from app.api.web import router as web_router
from app.services.monitor import AlertLevel, MonitorService


# ── Dummy lifespan (prevents full app startup) ────────────────────────────────

def _dummy_lifespan(app: FastAPI):
    @asynccontextmanager
    async def _inner(app):
        yield
    return _inner(app)


# ── Test app factory ──────────────────────────────────────────────────────────

def _make_test_app(monitor=None) -> FastAPI:
    app = FastAPI(lifespan=_dummy_lifespan)
    app.include_router(web_router)
    if monitor is not None:
        app.state.monitor = monitor
    return app


# ── HTML page ─────────────────────────────────────────────────────────────────

class TestServeDashboard:
    def test_get_root_returns_200(self):
        client = TestClient(_make_test_app())
        resp = client.get("/")
        assert resp.status_code == 200

    def test_content_type_is_html(self):
        client = TestClient(_make_test_app())
        resp = client.get("/")
        assert "text/html" in resp.headers["content-type"]

    def test_html_contains_lighthouse_title(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "Lighthouse" in html

    def test_html_contains_chartjs(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "chart.js" in html.lower() or "Chart.js" in html or "cdn.jsdelivr.net" in html

    def test_html_contains_bots_table(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "bots-table" in html or "Active Bots" in html

    def test_html_contains_trades_table(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "trades" in html.lower()

    def test_html_contains_equity_chart(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "equity" in html.lower()

    def test_html_contains_kill_switch_button(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "kill" in html.lower() or "Kill" in html

    def test_html_contains_alerts_section(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "alert" in html.lower()

    def test_html_is_single_file_self_contained(self):
        """The HTML file should have inline CSS and JS (no external CSS/JS except CDN)."""
        client = TestClient(_make_test_app())
        html = client.get("/").text
        # Should have <style> block
        assert "<style>" in html
        # Should have <script> block with JS logic
        assert "<script>" in html
        # Should not reference external CSS files
        assert '.css"' not in html

    def test_html_has_dark_background(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        # Dark theme — should have a dark colour variable or background
        assert "#0d1117" in html or "dark" in html.lower() or "--bg" in html

    def test_html_is_mobile_responsive(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "viewport" in html
        assert "@media" in html

    def test_html_autorefresh_30s(self):
        client = TestClient(_make_test_app())
        html = client.get("/").text
        assert "30_000" in html or "30000" in html

    def test_503_when_html_missing(self, tmp_path):
        """If the HTML file doesn't exist, return 503."""
        nonexistent = tmp_path / "missing.html"
        with patch.object(web_router_module, "_HTML_PATH", nonexistent):
            client = TestClient(_make_test_app())
            resp = client.get("/")
        assert resp.status_code == 503


# ── Monitor alerts endpoint ───────────────────────────────────────────────────

class TestMonitorAlertsEndpoint:
    def _make_monitor_with_alerts(self, alerts):
        m = MagicMock(spec=MonitorService)
        m.get_alert_history.return_value = alerts
        return m

    def test_returns_200_no_monitor(self):
        """Returns 200 with empty list when monitor not wired."""
        client = TestClient(_make_test_app(monitor=None))
        resp = client.get("/monitor/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["alerts"] == []
        assert data["total"] == 0

    def test_returns_alerts_from_monitor(self):
        alert = {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "level":      AlertLevel.WARNING,
            "check":      "kill_switch",
            "message":    "Kill switch is ACTIVE",
        }
        monitor = self._make_monitor_with_alerts([alert])
        client  = TestClient(_make_test_app(monitor=monitor))
        resp    = client.get("/monitor/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["alerts"][0]["level"] == AlertLevel.WARNING

    def test_returns_empty_when_no_alerts(self):
        monitor = self._make_monitor_with_alerts([])
        client  = TestClient(_make_test_app(monitor=monitor))
        resp    = client.get("/monitor/alerts")
        data    = resp.json()
        assert data["total"] == 0
        assert data["alerts"] == []

    def test_response_has_alerts_and_total_keys(self):
        monitor = self._make_monitor_with_alerts([])
        client  = TestClient(_make_test_app(monitor=monitor))
        data    = client.get("/monitor/alerts").json()
        assert "alerts" in data
        assert "total" in data

    def test_multiple_alerts_all_returned(self):
        alerts = [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level":     AlertLevel.CRITICAL,
                "check":     "drawdown",
                "message":   f"Drawdown alert {i}",
            }
            for i in range(5)
        ]
        monitor = self._make_monitor_with_alerts(alerts)
        client  = TestClient(_make_test_app(monitor=monitor))
        data    = client.get("/monitor/alerts").json()
        assert data["total"] == 5

    def test_monitor_called_with_limit_100(self):
        monitor = self._make_monitor_with_alerts([])
        client  = TestClient(_make_test_app(monitor=monitor))
        client.get("/monitor/alerts")
        monitor.get_alert_history.assert_called_once_with(limit=100)


# ── Integration: both routes on same app ──────────────────────────────────────

class TestWebRouterIntegration:
    def test_both_routes_registered(self):
        """GET / and GET /monitor/alerts should both be reachable."""
        client = TestClient(_make_test_app())
        assert client.get("/").status_code == 200
        assert client.get("/monitor/alerts").status_code == 200

    def test_root_is_html_alerts_is_json(self):
        client = TestClient(_make_test_app())
        root_ct  = client.get("/").headers["content-type"]
        alert_ct = client.get("/monitor/alerts").headers["content-type"]
        assert "text/html" in root_ct
        assert "application/json" in alert_ct

    def test_full_app_with_main_router(self):
        """Ensure web router integrates cleanly with create_app()."""
        from main import create_app

        app = create_app()
        with patch("main.lifespan", new_callable=lambda: _dummy_lifespan):
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get("/")
                # Root now serves the dashboard (may be HTML or 503 if monitor not wired)
                assert resp.status_code in (200, 503)
