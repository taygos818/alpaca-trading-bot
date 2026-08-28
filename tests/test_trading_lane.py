import importlib.util
import os
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
SPEC = importlib.util.spec_from_file_location("trading_lane", SERVICE_DIR / "trading_lane.py")
trading_lane = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(trading_lane)
TradingLanePolicy = trading_lane.TradingLanePolicy


class TradingLaneTests(unittest.TestCase):
    def policy_for(self, name):
        previous = os.environ.get("TRADING_LANE")
        os.environ["TRADING_LANE"] = name
        try:
            return TradingLanePolicy.from_env()
        finally:
            if previous is None:
                os.environ.pop("TRADING_LANE", None)
            else:
                os.environ["TRADING_LANE"] = previous

    def test_options_paper_lane_allows_options_strategy(self):
        policy = self.policy_for("options_paper")
        self.assertTrue(policy.supports_strategy("tier1_wheel"))
        self.assertTrue(policy.supports_strategy("defined_risk_options"))
        self.assertTrue(policy.supports_action("buy_to_open"))
        self.assertFalse(policy.supports_strategy("tier2_swing"))

    def test_live_lanes_are_not_defined(self):
        for lane in ("stock_live", "options_live"):
            with self.subTest(lane=lane), self.assertRaisesRegex(ValueError, "Unsupported TRADING_LANE"):
                self.policy_for(lane)

    def test_disabled_lane_blocks_everything(self):
        policy = self.policy_for("disabled")
        self.assertIn("not enabled", policy.explain_intent_block("tier1_wheel", "buy_to_open"))


if __name__ == "__main__":
    unittest.main()
