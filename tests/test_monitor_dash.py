import importlib.util
import sys
import unittest
from pathlib import Path

# Add services/monitor-dash to python path
SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "monitor-dash"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

# Dynamic import app
SPEC = importlib.util.spec_from_file_location("monitor_dash", SERVICE_DIR / "app.py")
monitor_dash = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor_dash)


class MonitorDashTests(unittest.TestCase):
    def setUp(self):
        self.app = monitor_dash.app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_metrics_endpoint_basic(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        body = response.data.decode("utf-8")
        
        # Check that the Prometheus exposition metrics exist
        self.assertIn("trades_total", body)
        self.assertIn("gross_notional_usd", body)
        self.assertIn("trades_allowed_total", body)
        self.assertIn("trades_blocked_total", body)
        self.assertIn("engine_cycle_duration_seconds", body)
        self.assertIn("engine_last_heartbeat_timestamp", body)
        self.assertIn("engine_cycles_total", body)
        
        # Check that they end with a newline
        self.assertTrue(body.endswith("\n"))

    def test_alpaca_info_endpoint(self):
        from unittest.mock import patch
        with patch.object(monitor_dash, "fetch_alpaca_account") as mock_account, \
             patch.object(monitor_dash, "fetch_alpaca_positions") as mock_positions:
            
            mock_account.return_value = {
                "equity": "100097.53",
                "cash": "85989.85",
                "buying_power": "383460.90",
                "portfolio_value": "100097.53",
                "account_number": "PA3XT0PBVZP8",
            }
            mock_positions.return_value = [
                {
                    "symbol": "AAPL",
                    "qty": "48",
                    "market_value": "14107.68",
                    "avg_entry_price": "291.50",
                    "current_price": "293.91",
                    "unrealized_intraday_pnl": "10.50",
                    "unrealized_pnl": "115.68",
                    "unrealized_pnl_pct": "0.0082",
                }
            ]

            response = self.client.get("/alpaca_info")
            self.assertEqual(response.status_code, 200)
            data = response.json
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["account"]["equity"], 100097.53)
            self.assertEqual(data["account"]["cash"], 85989.85)
            self.assertEqual(data["account"]["total_unrealized_pnl"], 115.68)
            self.assertEqual(len(data["positions"]), 1)
            self.assertEqual(data["positions"][0]["symbol"], "AAPL")
            self.assertEqual(data["positions"][0]["qty"], 48.0)

    def test_dashboard_has_no_mutation_routes(self):
        routes = {(rule.rule, tuple(sorted(rule.methods))) for rule in self.app.url_map.iter_rules()}
        route_paths = {path for path, _methods in routes}

        self.assertNotIn("/api/service/start", route_paths)
        self.assertNotIn("/api/service/stop", route_paths)
        self.assertNotIn("/api/order/submit", route_paths)
        self.assertNotIn("/api/position/close/<symbol>", route_paths)


if __name__ == "__main__":
    unittest.main()
