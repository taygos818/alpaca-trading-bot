import os
from dataclasses import dataclass


@dataclass
class RiskThresholds:
    max_trade_risk_pct: float = 0.05
    daily_drawdown_limit: float = 0.10
    max_concentration_pct: float = 0.40
    max_gross_exposure_pct: float = 0.90
    cash_buffer_pct: float = 0.10
    max_concurrent_positions: int = 5
    max_order_quantity: float = 100.0
    max_order_notional: float = 10000.0


class RiskManager:
    def __init__(self, thresholds: RiskThresholds | None = None):
        self.thresholds = thresholds or RiskThresholds()

    @classmethod
    def from_env(cls):
        thresholds = RiskThresholds(
            max_trade_risk_pct=float(os.getenv("MAX_TRADE_RISK_PCT", "0.05")),
            daily_drawdown_limit=float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.10")),
            max_concentration_pct=float(os.getenv("MAX_CONCENTRATION_PCT", "0.40")),
            max_gross_exposure_pct=float(os.getenv("MAX_GROSS_EXPOSURE_PCT", "0.90")),
            cash_buffer_pct=float(os.getenv("MIN_CASH_BUFFER_PCT", "0.10")),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "5")),
            max_order_quantity=float(os.getenv("MAX_ORDER_QUANTITY", "100")),
            max_order_notional=float(os.getenv("MAX_ORDER_NOTIONAL", "10000")),
        )
        return cls(thresholds)


    def check_order(
        self,
        symbol: str,
        quantity: float,
        order_value: float,
        account_nav: float,
        estimated_risk_value: float | None = None,
        current_position_value: float = 0.0,
        action: str = "buy",
        buying_power: float | None = None,
        available_cash: float | None = None,
        position_quantity: float | None = None,
        position_count: int = 0,
        gross_exposure: float = 0.0,
        open_order_exposure: float = 0.0,
        daily_pnl: float = 0.0,
        circuit_breaker_active: bool = False,
    ) -> tuple[bool, str]:
        if not symbol:
            return False, "symbol is required"
        if quantity <= 0:
            return False, "quantity must be positive"
        if order_value <= 0:
            return False, "order value must be positive"
        if account_nav <= 0:
            return False, "account NAV must be positive"
        if quantity > self.thresholds.max_order_quantity:
            return False, "order quantity exceeds independent maximum"
        if order_value > self.thresholds.max_order_notional:
            return False, "order notional exceeds independent maximum"

        # Closing orders bypass entry limits only after validating that the position exists.
        if action in ("sell", "sell_to_close", "buy_to_close", "buy_to_cover"):
            if position_quantity is None:
                return False, "position quantity is required for closing orders"
            if position_quantity == 0:
                return False, "cannot close a position that does not exist"
            if quantity > abs(position_quantity):
                return False, "close quantity exceeds current position quantity"
            return True, "approved"

        if action == "sell_short" and position_quantity not in (None, 0):
            return False, "short entry requires no existing position in the symbol"

        if circuit_breaker_active or self.check_circuit_breaker(daily_pnl, account_nav):
            return False, "persistent daily drawdown circuit breaker is active"

        if estimated_risk_value is None or estimated_risk_value < 0:
            return False, "valid estimated risk is required for exposure-increasing orders"
        risk_value = estimated_risk_value
        max_risk = account_nav * self.thresholds.max_trade_risk_pct
        if risk_value > max_risk:
            return False, f"trade risk {risk_value:.2f} exceeds limit {max_risk:.2f}"

        post_trade_exposure = current_position_value + order_value
        concentration_limit = account_nav * self.thresholds.max_concentration_pct
        if post_trade_exposure > concentration_limit:
            return (
                False,
                f"post-trade exposure {post_trade_exposure:.2f} exceeds concentration limit "
                f"{concentration_limit:.2f}",
            )

        if buying_power is not None and order_value > buying_power:
            return False, "order value exceeds broker buying power"
        if action != "sell_short" and available_cash is not None and available_cash - order_value < account_nav * self.thresholds.cash_buffer_pct:
            return False, "order would violate minimum cash buffer"
        if current_position_value <= 0 and position_count >= self.thresholds.max_concurrent_positions:
            return False, "maximum concurrent position count reached"

        projected_gross = gross_exposure + open_order_exposure + order_value
        gross_limit = account_nav * self.thresholds.max_gross_exposure_pct
        if projected_gross > gross_limit:
            return False, f"projected gross exposure {projected_gross:.2f} exceeds limit {gross_limit:.2f}"

        return True, "approved"

    def check_circuit_breaker(self, daily_pnl: float, account_nav: float) -> bool:
        if account_nav <= 0:
            return True
        drawdown_pct = abs(daily_pnl) / account_nav if daily_pnl < 0 else 0.0
        return drawdown_pct >= self.thresholds.daily_drawdown_limit
