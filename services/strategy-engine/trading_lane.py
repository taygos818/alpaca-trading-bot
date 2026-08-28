import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TradingLanePolicy:
    name: str
    allowed_strategies: frozenset[str]
    allowed_actions: frozenset[str]
    description: str

    @classmethod
    def from_env(cls):
        lane = os.getenv("TRADING_LANE", "stock_paper").strip().lower()
        if lane == "stock_paper":
            return cls(
                name="stock_paper",
                allowed_strategies=frozenset({"tier2_swing", "tier4_opening"}),
                allowed_actions=frozenset({"buy", "sell"}),
                description="Stable stock-only paper lane centered on Tier 2 swing execution.",
            )
        if lane == "stock_short_paper":
            return cls(
                name="stock_short_paper",
                allowed_strategies=frozenset({"tier4_opening"}),
                allowed_actions=frozenset({"sell_short", "buy_to_cover"}),
                description="Isolated, explicitly enabled paper-only short lane.",
            )
        if lane == "options_paper":
            return cls(
                name="options_paper",
                allowed_strategies=frozenset({"tier1_wheel"}),
                allowed_actions=frozenset({"sell_to_open", "sell_to_close", "buy_to_open", "buy_to_close", "buy", "sell"}),
                description="Guarded options-enabled paper lane centered on Tier 1 Wheel execution.",
            )
        if lane == "disabled":
            return cls(
                name="disabled",
                allowed_strategies=frozenset(),
                allowed_actions=frozenset(),
                description="Execution lane disabled for all strategies.",
            )
        raise ValueError(f"Unsupported TRADING_LANE: {lane}")

    def supports_strategy(self, strategy_name: str) -> bool:
        return strategy_name in self.allowed_strategies

    def supports_action(self, action: str) -> bool:
        return action in self.allowed_actions

    def validate_strategy(self, strategy_name: str):
        if not self.supports_strategy(strategy_name):
            raise ValueError(
                f"Strategy {strategy_name} is not enabled in TRADING_LANE={self.name}. "
                f"Allowed strategies: {sorted(self.allowed_strategies)}"
            )

    def explain_intent_block(self, strategy_name: str, action: str) -> str:
        if not self.supports_strategy(strategy_name):
            return (
                f"strategy {strategy_name} is not enabled in trading lane {self.name}"
            )
        if not self.supports_action(action):
            return f"action {action} is not enabled in trading lane {self.name}"
        return ""
