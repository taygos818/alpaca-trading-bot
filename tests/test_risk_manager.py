import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "strategy-engine" / "risk_manager.py"
SPEC = importlib.util.spec_from_file_location("risk_manager", MODULE_PATH)
risk_manager = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(risk_manager)

RiskManager = risk_manager.RiskManager
RiskThresholds = risk_manager.RiskThresholds


def make_manager():
    return RiskManager(
        RiskThresholds(
            max_trade_risk_pct=0.01,
            daily_drawdown_limit=0.03,
            max_concentration_pct=0.15,
            max_order_quantity=1000,
            max_order_notional=1000000,
        )
    )


def test_allows_order_inside_limits():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=1,
        order_value=1000,
        account_nav=100000,
        estimated_risk_value=500,
        current_position_value=4000,
    )
    assert allowed is True
    assert reason == "approved"


def test_blocks_trade_risk_above_limit():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=1,
        order_value=1000,
        account_nav=100000,
        estimated_risk_value=1200,
        current_position_value=4000,
    )
    assert allowed is False
    assert "trade risk" in reason


def test_blocks_concentration_above_limit():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=2,
        order_value=2000,
        account_nav=10000,
        estimated_risk_value=50,
        current_position_value=1000,
    )
    assert allowed is False
    assert "concentration" in reason


def test_allows_sell_above_limits():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=2,
        order_value=2000,
        account_nav=1000,
        estimated_risk_value=50,
        current_position_value=1000,
        action="sell",
        position_quantity=3,
    )
    assert allowed is True
    assert reason == "approved"


def test_allows_sell_to_close_above_limits():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=2,
        order_value=2000,
        account_nav=1000,
        estimated_risk_value=50,
        current_position_value=1000,
        action="sell_to_close",
        position_quantity=3,
    )
    assert allowed is True
    assert reason == "approved"


def test_short_entry_uses_risk_and_buying_power_but_not_cash_buffer():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY", quantity=2, order_value=2000, account_nav=100000,
        estimated_risk_value=500, action="sell_short", position_quantity=0,
        buying_power=5000, available_cash=0,
    )
    assert allowed is True
    assert reason == "approved"


def test_short_entry_rejects_existing_position():
    allowed, reason = make_manager().check_order(
        symbol="SPY", quantity=1, order_value=1000, account_nav=100000,
        estimated_risk_value=100, action="sell_short", position_quantity=1,
    )
    assert allowed is False
    assert "no existing position" in reason


def test_circuit_breaker_triggers_on_daily_drawdown():
    manager = make_manager()
    assert manager.check_circuit_breaker(daily_pnl=-3500, account_nav=100000) is True


def test_circuit_breaker_allows_small_drawdown():
    manager = make_manager()
    assert manager.check_circuit_breaker(daily_pnl=-1000, account_nav=100000) is False


def test_recalibrated_500_dollar_account_limits():
    # Test $500 account defaults: 5% max risk ($25), 40% concentration ($200), 10% drawdown ($50)
    manager = RiskManager.from_env()
    assert manager.thresholds.max_trade_risk_pct == 0.05
    assert manager.thresholds.max_concentration_pct == 0.40
    assert manager.thresholds.daily_drawdown_limit == 0.10

    # $25 risk allowed on $500 account
    allowed, reason = manager.check_order(
        symbol="SPY",
        quantity=1,
        order_value=150,
        account_nav=500,
        estimated_risk_value=25,
        current_position_value=0,
    )
    assert allowed is True

    # $30 risk (> $25 max) blocked
    allowed_blocked, reason_blocked = manager.check_order(
        symbol="SPY",
        quantity=1,
        order_value=150,
        account_nav=500,
        estimated_risk_value=30,
        current_position_value=0,
    )
    assert allowed_blocked is False
    assert "trade risk" in reason_blocked

    # $50 daily loss triggers circuit breaker (10% of $500)
    assert manager.check_circuit_breaker(daily_pnl=-50.0, account_nav=500) is True
    assert manager.check_circuit_breaker(daily_pnl=-20.0, account_nav=500) is False


def test_blocks_close_larger_than_position():
    allowed, reason = make_manager().check_order(
        symbol="SPY", quantity=3, order_value=300, account_nav=10000,
        estimated_risk_value=0, action="sell", position_quantity=2,
    )
    assert allowed is False
    assert "exceeds" in reason


def test_blocks_entry_when_buying_power_or_position_limit_is_exceeded():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY", quantity=1, order_value=1000, account_nav=100000,
        estimated_risk_value=100, buying_power=500,
    )
    assert allowed is False and "buying power" in reason
    allowed, reason = manager.check_order(
        symbol="NEW", quantity=1, order_value=1000, account_nav=100000,
        estimated_risk_value=100, position_count=5,
    )
    assert allowed is False and "position count" in reason


def test_persistent_circuit_breaker_blocks_new_risk_but_allows_close():
    manager = make_manager()
    allowed, reason = manager.check_order(
        symbol="SPY", quantity=1, order_value=1000, account_nav=100000,
        estimated_risk_value=100, circuit_breaker_active=True,
    )
    assert allowed is False and "circuit breaker" in reason
    allowed, _ = manager.check_order(
        symbol="SPY", quantity=1, order_value=1000, account_nav=100000,
        estimated_risk_value=100, circuit_breaker_active=True,
        action="sell", position_quantity=1,
    )
    assert allowed is True
