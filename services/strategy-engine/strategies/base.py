from dataclasses import dataclass
from datetime import datetime, timezone
import os


@dataclass
class TradeIntent:
    strategy: str
    symbol: str
    action: str
    quantity: float
    order_value: float
    account_nav: float
    estimated_risk_value: float | None = None
    current_position_value: float = 0.0
    notes: str = ""
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    signal_timestamp: str = ""
    config_version: str = ""
    data_provenance: list[dict] | None = None
    reference_price: float = 0.0

    def __post_init__(self):
        if not self.signal_timestamp:
            self.signal_timestamp = datetime.now(timezone.utc).isoformat()
        if not self.config_version:
            self.config_version = os.getenv("STRATEGY_CONFIG_VERSION", "unversioned")
        if self.reference_price <= 0 and self.quantity > 0 and self.order_value > 0:
            self.reference_price = self.order_value / self.quantity


class BaseStrategy:
    name = "base"

    def run_cycle(self) -> TradeIntent:
        raise NotImplementedError
