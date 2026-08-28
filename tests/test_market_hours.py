import importlib.util
import os
import sys
import unittest
from unittest.mock import ANY, patch, MagicMock
from pathlib import Path
from types import SimpleNamespace

# Add service paths to sys.path
SCHEDULER_DIR = Path(__file__).resolve().parents[1] / "services" / "scheduler"
if str(SCHEDULER_DIR) not in sys.path:
    sys.path.insert(0, str(SCHEDULER_DIR))

BOT_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

# Dynamic imports
SCHED_SPEC = importlib.util.spec_from_file_location("scheduler", SCHEDULER_DIR / "scheduler.py")
scheduler = importlib.util.module_from_spec(SCHED_SPEC)
assert SCHED_SPEC.loader is not None
SCHED_SPEC.loader.exec_module(scheduler)
sys.modules["scheduler"] = scheduler

BOT_SPEC = importlib.util.spec_from_file_location("bot", BOT_DIR / "bot.py")
bot = importlib.util.module_from_spec(BOT_SPEC)
assert BOT_SPEC.loader is not None
BOT_SPEC.loader.exec_module(bot)
sys.modules["bot"] = bot


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code
        self.text = "Error response text"

    def json(self):
        return self.json_data


class MarketHoursSchedulerTests(unittest.TestCase):
    @patch("scheduler.redis.Redis.from_url")
    @patch("scheduler.requests.get")
    def test_check_market_hours_open(self, mock_get, mock_redis_from_url):
        # Mock requests.get to return is_open: True
        mock_get.return_value = FakeResponse({"is_open": True})
        
        # Mock redis client
        mock_redis_client = MagicMock()
        mock_redis_from_url.return_value = mock_redis_client

        # Set env vars
        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test-key",
            "ALPACA_SECRET_KEY": "test-secret",
            "REDIS_URL": "redis://mock:6379/0"
        }):
            # Reset global client to force re-connection
            scheduler.redis_client = None
            scheduler.check_market_hours()

        pipeline = mock_redis_client.pipeline.return_value
        pipeline.set.assert_any_call("market_open", "true")
        pipeline.set.assert_any_call("market_open_updated_at", ANY)
        pipeline.setnx.assert_called_once_with("bot_paused", "true")
        pipeline.execute.assert_called_once_with()

    @patch("scheduler.redis.Redis.from_url")
    @patch("scheduler.requests.get")
    def test_check_market_hours_closed(self, mock_get, mock_redis_from_url):
        # Mock requests.get to return is_open: False
        mock_get.return_value = FakeResponse({"is_open": False})
        
        # Mock redis client
        mock_redis_client = MagicMock()
        mock_redis_from_url.return_value = mock_redis_client

        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test-key",
            "ALPACA_SECRET_KEY": "test-secret",
            "REDIS_URL": "redis://mock:6379/0"
        }):
            scheduler.redis_client = None
            scheduler.check_market_hours()

        mock_redis_client.pipeline.return_value.set.assert_any_call("market_open", "false")

    @patch("scheduler.redis.Redis.from_url")
    @patch("scheduler.requests.get")
    def test_check_market_hours_api_failure_fallback(self, mock_get, mock_redis_from_url):
        # Mock requests.get to return status 500 (failure)
        mock_get.return_value = FakeResponse({}, status_code=500)
        
        # Mock redis client
        mock_redis_client = MagicMock()
        mock_redis_from_url.return_value = mock_redis_client

        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test-key",
            "ALPACA_SECRET_KEY": "test-secret",
            "REDIS_URL": "redis://mock:6379/0"
        }):
            scheduler.redis_client = None
            scheduler.check_market_hours()

        # Clock failures must fail closed.
        mock_redis_client.pipeline.return_value.set.assert_any_call("market_open", "false")


class MarketHoursBotTests(unittest.TestCase):
    def test_protected_symbols_are_blocked_from_automation(self):
        with patch.dict(os.environ, {"PROTECTED_SYMBOLS": "AMD,SPY,TSLA"}):
            self.assertIn("human-managed", bot.protected_symbol_block_reason("spy"))
            self.assertEqual(bot.protected_symbol_block_reason("QQQ"), "")

    def make_mock_intent(self, action="buy"):
        return SimpleNamespace(
            action=action,
            strategy="tier2_swing",
            symbol="SPY",
            quantity=10,
            order_value=1000.0,
            estimated_risk_value=100.0,
            account_nav=100000.0,
            current_position_value=0.0
        )

    @patch("bot.TradingLanePolicy.from_env")
    @patch("bot.validate_startup_readiness")
    @patch("bot.build_strategy")
    @patch("bot.RiskManager.from_env")
    @patch("bot.DiscordNotifier.from_env")
    @patch("bot.TradeStore.from_env")
    @patch("bot.AlpacaBrokerStateClient.from_env")
    @patch("bot.AlpacaOrderExecutor.from_env")
    @patch("bot.HeartbeatWriter")
    @patch("bot.redis.Redis.from_url")
    @patch("time.sleep", side_effect=KeyboardInterrupt)  # Interrupt the loop immediately
    def test_bot_blocks_when_market_closed(self, mock_sleep, mock_redis_from_url, 
                                           mock_heartbeat, mock_executor, mock_broker_state, 
                                           mock_store, mock_notifier, mock_risk_manager, 
                                           mock_build_strategy, mock_validate, mock_policy):
        # Setup mock Redis client returning "false"
        mock_redis_client = MagicMock()
        mock_redis_client.get.return_value = "false"
        mock_redis_from_url.return_value = mock_redis_client

        # Setup strategy to return buy intent
        mock_strategy = MagicMock()
        mock_strategy.name = "tier2_swing"
        mock_strategy.run_cycle.return_value = self.make_mock_intent(action="buy")
        mock_build_strategy.return_value = mock_strategy

        # Setup policy to allow everything initially
        mock_policy_inst = MagicMock()
        mock_policy_inst.explain_intent_block.return_value = None
        mock_policy.return_value = mock_policy_inst

        # Setup trade store and notifier
        mock_store_inst = MagicMock()
        mock_store.return_value = mock_store_inst
        mock_notifier_inst = MagicMock()
        mock_notifier.return_value = mock_notifier_inst

        with patch.dict(os.environ, {"REDIS_URL": "redis://mock:6379/0"}):
            try:
                bot.main()
            except KeyboardInterrupt:
                pass

        # Assert store logged a blocked trade event with reason "market is closed"
        mock_store_inst.log_trade_event.assert_called_once()
        logged_event = mock_store_inst.log_trade_event.call_args[0][0]
        self.assertFalse(logged_event["allowed"])
        self.assertEqual(logged_event["reason"], "market is closed")

        # Assert notifier sent a message explaining the block
        mock_notifier_inst.send.assert_called_once()
        self.assertIn("reason=market is closed", mock_notifier_inst.send.call_args[0][0])

        # Assert executor was never called
        mock_executor.return_value.execute.assert_not_called()

    @patch("bot.TradingLanePolicy.from_env")
    @patch("bot.validate_startup_readiness")
    @patch("bot.build_strategy")
    @patch("bot.RiskManager.from_env")
    @patch("bot.DiscordNotifier.from_env")
    @patch("bot.TradeStore.from_env")
    @patch("bot.AlpacaBrokerStateClient.from_env")
    @patch("bot.AlpacaOrderExecutor.from_env")
    @patch("bot.HeartbeatWriter")
    @patch("bot.redis.Redis.from_url")
    @patch("time.sleep", side_effect=KeyboardInterrupt)  # Interrupt the loop immediately
    def test_bot_allows_when_market_open(self, mock_sleep, mock_redis_from_url, 
                                          mock_heartbeat, mock_executor, mock_broker_state, 
                                          mock_store, mock_notifier, mock_risk_manager, 
                                          mock_build_strategy, mock_validate, mock_policy):
        # Setup mock Redis client: bot_paused="false", market_open="true"
        mock_redis_client = MagicMock()
        mock_redis_client.get.side_effect = lambda key: "false" if key == "bot_paused" else "true"
        mock_redis_from_url.return_value = mock_redis_client

        # Setup strategy to return buy intent
        mock_strategy = MagicMock()
        mock_strategy.name = "tier2_swing"
        mock_strategy.run_cycle.return_value = self.make_mock_intent(action="buy")
        mock_build_strategy.return_value = mock_strategy

        # Setup policy to allow everything
        mock_policy_inst = MagicMock()
        mock_policy_inst.explain_intent_block.return_value = None
        mock_policy.return_value = mock_policy_inst

        # Setup risk manager to allow trade
        mock_risk_inst = MagicMock()
        mock_risk_inst.check_order.return_value = (True, "")
        mock_risk_manager.return_value = mock_risk_inst

        # Setup broker state to not be blocked
        mock_broker_state.return_value.fetch.return_value.trading_blocked = False

        # Setup executor
        mock_executor_inst = MagicMock()
        mock_executor.return_value = mock_executor_inst

        with patch.dict(os.environ, {"REDIS_URL": "redis://mock:6379/0"}):
            try:
                bot.main()
            except KeyboardInterrupt:
                pass

        # Assert store logged trade event allowing it
        mock_store_inst = mock_store.return_value
        mock_store_inst.log_trade_event.assert_called_once()
        logged_event = mock_store_inst.log_trade_event.call_args[0][0]
        self.assertTrue(logged_event["allowed"])

        # Assert executor was called
        mock_executor_inst.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
