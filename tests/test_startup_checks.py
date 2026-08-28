import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

LANE_SPEC = importlib.util.spec_from_file_location("trading_lane", SERVICE_DIR / "trading_lane.py")
trading_lane = importlib.util.module_from_spec(LANE_SPEC)
assert LANE_SPEC.loader is not None
LANE_SPEC.loader.exec_module(trading_lane)

CHECK_SPEC = importlib.util.spec_from_file_location("startup_checks", SERVICE_DIR / "startup_checks.py")
startup_checks = importlib.util.module_from_spec(CHECK_SPEC)
assert CHECK_SPEC.loader is not None
CHECK_SPEC.loader.exec_module(startup_checks)

TradingLanePolicy = trading_lane.TradingLanePolicy
StartupReadinessError = startup_checks.StartupReadinessError
StartupSettings = startup_checks.StartupSettings
OperatingMode = startup_checks.OperatingMode
validate_startup_readiness = startup_checks.validate_startup_readiness


class StartupCheckTests(unittest.TestCase):
    def make_settings(self, **overrides):
        payload = {
            "trading_lane": "options_paper",
            "bot_strategy": "tier1_wheel",
            "alpaca_paper_trade": True,
            "alpaca_order_dry_run": True,
            "require_broker_state_for_trades": True,
            "paper_order_submission_enabled": False,
            "alpaca_api_key": "key",
            "alpaca_secret_key": "secret",
            "active_broker": "alpaca",
        }
        payload.update(overrides)
        return StartupSettings(**payload)

    def make_policy(self):
        return TradingLanePolicy(
            name="options_paper",
            allowed_strategies=frozenset({"tier1_wheel"}),
            allowed_actions=frozenset({"buy_to_open", "sell_to_close"}),
            description="Paper options lane",
        )

    def test_valid_options_paper_settings_pass(self):
        validate_startup_readiness(self.make_settings(), self.make_policy())

    def test_project_permanently_rejects_live_mode(self):
        with self.assertRaisesRegex(StartupReadinessError, "paper-only"):
            validate_startup_readiness(
                self.make_settings(
                    mode=OperatingMode.LIVE,
                    trading_lane="options_live",
                    alpaca_paper_trade=False,
                    trading_api_url="https://api.alpaca.markets",
                    alpaca_order_dry_run=False,
                ),
                TradingLanePolicy("options_live", frozenset({"tier1_wheel"}), frozenset({"buy_to_open"}), "invalid"),
            )

    def test_project_rejects_non_alpaca_broker(self):
        with self.assertRaisesRegex(StartupReadinessError, "ACTIVE_BROKER=alpaca"):
            validate_startup_readiness(self.make_settings(active_broker="robinhood"), self.make_policy())

    def test_paper_mode_rejects_live_endpoint(self):
        with self.assertRaisesRegex(StartupReadinessError, "paper credentials"):
            validate_startup_readiness(
                self.make_settings(trading_api_url="https://api.alpaca.markets"), self.make_policy()
            )

    def test_disabling_dry_run_requires_explicit_paper_enable(self):
        with self.assertRaisesRegex(StartupReadinessError, "PAPER_ORDER_SUBMISSION_ENABLED"):
            validate_startup_readiness(
                self.make_settings(alpaca_order_dry_run=False, paper_order_submission_enabled=False),
                self.make_policy(),
            )

    def test_missing_credentials_fail(self):
        with self.assertRaisesRegex(StartupReadinessError, "must be configured"):
            validate_startup_readiness(
                self.make_settings(alpaca_api_key="", alpaca_secret_key=""), self.make_policy()
            )

    def test_offline_mode_accepts_disabled_lane_without_credentials(self):
        disabled = TradingLanePolicy("disabled", frozenset(), frozenset(), "disabled")
        validate_startup_readiness(
            self.make_settings(
                mode=OperatingMode.OFFLINE,
                trading_lane="disabled",
                bot_strategy="none",
                alpaca_api_key="",
                alpaca_secret_key="",
            ),
            disabled,
        )

    def test_invalid_strategy_config_fails_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid_path = Path(temp_dir) / "strategy.toml"
            invalid_path.write_text("[swing\ninvalid", encoding="utf-8")
            with self.assertRaisesRegex(StartupReadinessError, "Invalid strategy configuration"):
                validate_startup_readiness(
                    self.make_settings(strategy_config_path=str(invalid_path)), self.make_policy()
                )

    def test_environment_factories_reject_live_credentials(self):
        with patch.dict(os.environ, {"ALPACA_PAPER_TRADE": "false"}, clear=False):
            with self.assertRaisesRegex(ValueError, "paper-only"):
                startup_checks.StartupSettings.from_env()


if __name__ == "__main__":
    unittest.main()
