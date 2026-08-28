import importlib.util
import json
import sys
import tempfile
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
        self.temp_dir = tempfile.TemporaryDirectory()
        monitor_dash.DECISION_TRACE_PATH = Path(self.temp_dir.name) / "decision-traces.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

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
            self.assertNotIn("account_number", data["account"])
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

    def test_agent_decision_api_is_read_only_explainable_and_redacted(self):
        record = {
            "trace_id": "trace.dashboard.001",
            "phase": "broker_reconciled",
            "outcome": "submitted",
            "fingerprint": "a" * 64,
            "recorded_at": "2026-08-28T16:01:00Z",
            "metadata": {"opportunity_rankings": [{"symbol": "ANF", "rank": 1}]},
            "trace": {
                "evidence": [{
                    "provider": "finnhub", "instrument": "ANF", "value_name": "company_news",
                    "value": "raw content is not public", "source_uri": "https://example.test/news",
                    "event_time": "2026-08-28T15:55:00Z", "received_at": "2026-08-28T15:56:00Z",
                    "entitlement": "contest", "authority": "licensed_research", "is_fresh": True,
                    "alpaca_secret_key": "never-return-this",
                }],
                "analyses": [{
                    "agent_name": "technical", "direction": "bullish", "confidence": "0.70",
                    "disposition": "analyze", "thesis": "Completed bars confirm momentum.",
                    "contradictions": [], "cited_evidence_ids": ["evidence.001"],
                }],
                "proposals": [{
                    "record_id": "proposal.001", "underlying": "ANF", "direction": "bullish",
                    "strategy_name": "call_debit_spread", "contract_quantity": 1,
                    "limit_debit": "1.00", "maximum_loss": "100", "rationale": "Known risk.",
                    "legs": [{"option_symbol": "ANF-CALL", "side": "buy", "right": "call", "strike": "100", "expiration": "2026-09-11"}],
                }],
                "objections": [],
                "authorizations": [{
                    "proposal_id": "proposal.001", "decision": "approve", "authorized_quantity": 1,
                    "authorized_maximum_loss": "100", "reason": "Within limits.", "expires_at": "2026-08-28T16:02:00Z",
                }],
                "order_events": [{"status": "filled", "filled_quantity": 1, "average_fill_price": "0.98", "broker_timestamp": "2026-08-28T16:01:00Z", "broker_order_id": "sensitive-id"}],
                "assessments": [{"position_key": "position.ANF", "state": "open", "quantity": 1, "mark_value": "98", "unrealized_pnl": "0", "exit_reasons": [], "assessed_at": "2026-08-28T16:01:00Z"}],
            },
        }
        monitor_dash.DECISION_TRACE_PATH.write_text(json.dumps(record) + "\n", encoding="utf-8")
        response = self.client.get("/api/agent-decisions")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["count"], 1)
        public = body["records"][0]
        self.assertEqual(public["agents"][0]["citation_count"], 1)
        self.assertEqual(public["risk_decisions"][0]["decision"], "approve")
        encoded = response.data.decode("utf-8")
        self.assertNotIn("never-return-this", encoded)
        self.assertNotIn("raw content is not public", encoded)
        self.assertNotIn("sensitive-id", encoded)

        detail = self.client.get("/api/agent-decisions/trace.dashboard.001")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(self.client.get("/api/agent-decisions/../../etc/passwd").status_code, 404)

    def test_agents_page_truthfully_handles_empty_journal(self):
        response = self.client.get("/agents")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Taygos818's Alpaca Hackathon Dashboard", response.data)
        self.assertIn(b'id="traceSearch"', response.data)
        self.assertIn(b'id="outcomeFilter"', response.data)
        self.assertIn(b'id="inspector"', response.data)
        self.assertIn(b"PAPER ACCOUNT ONLY", response.data)
        payload = self.client.get("/api/agent-decisions").get_json()
        self.assertEqual(payload, {"status": "ok", "count": 0, "records": []})


if __name__ == "__main__":
    unittest.main()
