import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT_DIR / "services" / "scheduler"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import scheduler as scheduler_module
from scheduler import send_daily_performance_report


class TestSchedulerDailyReport(unittest.TestCase):
    def setUp(self):
        self.env_patches = {
            "ALPACA_API_KEY": "fake_key",
            "ALPACA_SECRET_KEY": "fake_secret",
            "ALPACA_TRADING_API_URL": "https://paper-api.alpaca.markets",
            "POSTGRES_DSN": "postgresql://postgres:postgres@localhost:5432/tradingbot",
            "SMTP_HOST": "smtp.mock.com",
            "SMTP_PORT": "587",
            "SMTP_FROM": "bot@mock.com",
            "SMTP_TO": "user@mock.com",
        }
        for k, v in self.env_patches.items():
            os.environ[k] = v

    def tearDown(self):
        for k in self.env_patches.keys():
            os.environ.pop(k, None)

    @patch("requests.get")
    @patch("psycopg.connect")
    @patch("notifications.EmailNotifier.send")
    def test_send_daily_performance_report_success(self, mock_notifier_send, mock_db_connect, mock_get):
        # 1. Mock Alpaca Account GET
        mock_acct_resp = MagicMock()
        mock_acct_resp.status_code = 200
        mock_acct_resp.json.return_value = {
            "equity": "105000.50",
            "cash": "5000.50",
            "long_market_value": "100000.00",
            "short_market_value": "0.00",
        }
        
        # 2. Mock Alpaca Positions GET
        mock_pos_resp = MagicMock()
        mock_pos_resp.status_code = 200
        mock_pos_resp.json.return_value = [
            {
                "symbol": "AAPL",
                "qty": "50",
                "avg_entry_price": "190.00",
                "current_price": "200.00",
                "market_value": "10000.00",
                "unrealized_pl": "500.00",
                "unrealized_plpc": "0.0526"
            }
        ]

        # Map mock GET calls
        def mock_get_route(url, *args, **kwargs):
            if "account" in url:
                return mock_acct_resp
            if "positions" in url:
                return mock_pos_resp
            return MagicMock()

        mock_get.side_effect = mock_get_route

        # 3. Mock Database Connection & Cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_db_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock query results:
        # a. 24h NAV snapshot (prev_nav = 100000.00)
        # b. executed trades (1 buy order)
        mock_cursor.fetchone.return_value = (100000.00,)
        
        t_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_cursor.fetchall.return_value = [
            (t_time, "tier2_swing", "AAPL", "buy", 50, "submitted", "order-123", "")
        ]

        # Run report
        send_daily_performance_report()

        # Check DB calls
        self.assertTrue(mock_db_connect.called)
        
        # Check notifier send
        mock_notifier_send.assert_called_once()
        subject = mock_notifier_send.call_args[1]["subject"]
        body = mock_notifier_send.call_args[1]["body"]
        html_body = mock_notifier_send.call_args[1]["html_body"]

        # Validate subject: 105,000.50 current nav, compared to 100,000.00 prev nav (+5.00%)
        self.assertIn("105,000.50", subject)
        self.assertIn("+5.00%", subject)

        # Validate body and HTML
        self.assertIn("AAPL", body)
        self.assertIn("submitted", body)
        self.assertIn("Net Asset Value (NAV): $105,000.50", body)
        self.assertIn("24h Change: +$5,000.50", body)
        
        self.assertIn("AAPL", html_body)
        self.assertIn("<h1>DAILY PERFORMANCE SUMMARY</h1>", html_body)


class TestAlpacaConnectivityMonitoring(unittest.TestCase):
    def setUp(self):
        scheduler_module.ALPACA_CONSECUTIVE_FAILURES = 0
        scheduler_module.ALPACA_OUTAGE_ALERTED = False

    @patch.dict(os.environ, {"ALPACA_CONNECTIVITY_ALERT_THRESHOLD": "2"}, clear=False)
    @patch("scheduler.EmailNotifier.from_env")
    @patch("scheduler.DiscordNotifier.from_env")
    @patch("scheduler.get_redis_client")
    def test_sustained_failure_alerts_once_and_recovery_alerts(self, mock_redis, mock_discord, mock_email):
        redis_client = MagicMock()
        mock_redis.return_value = redis_client
        discord = mock_discord.return_value
        email = mock_email.return_value

        scheduler_module.record_alpaca_connectivity(False, "NameResolutionError")
        scheduler_module.record_alpaca_connectivity(False, "NameResolutionError")
        scheduler_module.record_alpaca_connectivity(False, "NameResolutionError")
        self.assertEqual(discord.send.call_count, 1)
        self.assertEqual(email.send.call_count, 1)

        scheduler_module.record_alpaca_connectivity(True)
        self.assertEqual(discord.send.call_count, 2)
        self.assertEqual(email.send.call_count, 2)
        self.assertEqual(
            redis_client.mset.call_args_list[-1].args[0]["alpaca_connectivity_status"],
            "ok",
        )


class TestEngineHeartbeatMonitoring(unittest.TestCase):
    def setUp(self):
        scheduler_module.ENGINE_HEARTBEAT_ALERTED = False

    @patch("scheduler.EmailNotifier.from_env")
    @patch("scheduler.DiscordNotifier.from_env")
    @patch("scheduler.get_redis_client")
    def test_missing_heartbeat_alerts_once_then_reports_recovery(self, mock_redis, mock_discord, mock_email):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine-heartbeat"
            with patch.dict(
                os.environ,
                {"ENGINE_HEARTBEAT_PATH": str(path), "ENGINE_HEARTBEAT_MAX_AGE_SECONDS": "90"},
                clear=False,
            ):
                self.assertFalse(scheduler_module.check_engine_heartbeat())
                self.assertFalse(scheduler_module.check_engine_heartbeat())
                self.assertEqual(mock_discord.return_value.send.call_count, 1)
                path.write_text(str(int(time.time())), encoding="utf-8")
                self.assertTrue(scheduler_module.check_engine_heartbeat())
                self.assertEqual(mock_discord.return_value.send.call_count, 2)
                self.assertEqual(mock_email.return_value.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
