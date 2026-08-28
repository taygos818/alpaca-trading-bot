import logging
import os
import time
from datetime import datetime

import redis

from risk_manager import RiskManager
from strategies.tier1_wheel import WheelStrategy
from strategies.tier2_swing import SwingStrategy
from strategies.tier3_intraday import IntradayStrategy
from strategies.tier4_opening import OpeningOpportunityStrategy
from strategies.opening_swing import OpeningThenSwingStrategy
from startup_checks import OperatingMode, StartupSettings, validate_runtime_dependencies, validate_startup_readiness
from trading_lane import TradingLanePolicy
from utils.broker_state import AlpacaBrokerStateClient, BrokerStateError
from utils.execution import AlpacaOrderExecutor, OrderExecutionError
from utils.fractional_protection import (
    FractionalProtectionCoordinator,
    FractionalProtectionError,
    is_fractional,
)
from utils.order_lifecycle import (
    OrderLifecycleCoordinator,
    OrderReconciliationRequired,
    deterministic_intent_id,
)
from utils.heartbeat import HeartbeatWriter
from utils.notifications import DiscordNotifier, EmailNotifier
from utils.storage import TradeStore


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("strategy-engine")


def live_control_plane_block_reason(redis_client, settings: StartupSettings, now: float | None = None) -> str:
    mode_value = str(getattr(settings.mode, "value", settings.mode))
    if mode_value != OperatingMode.LIVE.value:
        return ""
    if redis_client is None:
        return "live control plane unavailable"

    current_time = time.time() if now is None else now
    try:
        paused = redis_client.get("bot_paused")
        market_open = redis_client.get("market_open")
        updated_at = redis_client.get("market_open_updated_at")
    except Exception as exc:
        return f"live control plane read failed: {type(exc).__name__}"

    if paused not in {"true", "false"}:
        return "live pause state missing or invalid"
    if paused == "true":
        return "service paused by user"
    if market_open not in {"true", "false"}:
        return "live market clock state missing or invalid"
    try:
        age = current_time - float(updated_at)
    except (TypeError, ValueError):
        return "live market clock timestamp missing or invalid"
    if age < 0 or age > settings.control_state_max_age_seconds:
        return "live market clock state is stale"
    if market_open == "false":
        return "market is closed"
    return ""


def circuit_breaker_state(redis_client, risk_manager, broker_state) -> bool:
    if redis_client is None:
        return False
    key = "risk:daily_drawdown_circuit_breaker"
    try:
        active = redis_client.get(key) == "true"
        daily_pnl = broker_state.account_nav - broker_state.last_equity
        if not active and risk_manager.check_circuit_breaker(daily_pnl, broker_state.account_nav):
            redis_client.set(key, "true")
            active = True
        return active
    except Exception as exc:
        LOGGER.error("Risk circuit-breaker state unavailable: %s", exc)
        return True


def intent_data_block_reason(intent, strategy, settings: StartupSettings, observations=None, now: float | None = None) -> str:
    if observations is None:
        observations = list(getattr(getattr(strategy, "data", None), "observations", [])[-20:])
    else:
        observations = list(observations)
    intent.data_provenance = observations
    if settings.mode is not OperatingMode.LIVE:
        return ""
    if not observations:
        return "live signal has no market-data provenance"
    current_time = time.time() if now is None else now
    max_age = float(os.getenv("MAX_MARKET_DATA_RETRIEVAL_AGE_SECONDS", "30"))
    for observation in observations:
        if observation.get("source") not in {"alpaca", "alpaca_cache"}:
            return f"unapproved live data source: {observation.get('source', 'unknown')}"
        if observation.get("quality") != "verified":
            return "market-data quality is not verified"
        try:
            retrieved = datetime.fromisoformat(str(observation["retrieved_at"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            return "market-data retrieval timestamp missing or invalid"
        age = current_time - retrieved
        if age < 0 or age > max_age:
            return "market data is stale"
    return ""


def protected_symbol_block_reason(symbol: str) -> str:
    protected = {
        value.strip().upper()
        for value in os.getenv("PROTECTED_SYMBOLS", "").split(",")
        if value.strip()
    }
    if symbol.upper() in protected:
        return f"{symbol.upper()} is a human-managed protected position"
    return ""


def build_strategy(policy: TradingLanePolicy):
    strategy_name = os.getenv("BOT_STRATEGY", "tier2_swing")
    strategies = {
        "tier1_wheel": WheelStrategy,
        "tier2_swing": SwingStrategy,
        "tier3_intraday": IntradayStrategy,
        "tier4_opening": OpeningOpportunityStrategy,
    }
    try:
        strategy_cls = strategies[strategy_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported BOT_STRATEGY: {strategy_name}") from exc
    policy.validate_strategy(strategy_name)
    if strategy_name == "tier2_swing" and os.getenv("OPENING_EXECUTION_ENABLED", "false").strip().lower() == "true":
        policy.validate_strategy("tier4_opening")
        return OpeningThenSwingStrategy()
    return strategy_cls()


def main():
    policy = TradingLanePolicy.from_env()
    startup_settings = StartupSettings.from_env()
    validate_startup_readiness(settings=startup_settings, policy=policy)
    if startup_settings.mode in {OperatingMode.OFFLINE, OperatingMode.BACKTEST}:
        LOGGER.info("Strategy engine is disabled in BOT_ENVIRONMENT=%s", startup_settings.mode.value)
        return
    validate_runtime_dependencies(startup_settings)
    strategy = build_strategy(policy)
    risk_manager = RiskManager.from_env()
    notifier = DiscordNotifier.from_env()
    email_notifier = EmailNotifier.from_env()
    alert_cooldowns = {}

    def send_email_alert(alert_key: str, subject: str, message: str):
        now = time.time()
        cooldown = float(os.getenv("EMAIL_ALERT_COOLDOWN_SECONDS", "300"))
        if alert_key in alert_cooldowns:
            if now - alert_cooldowns[alert_key] < cooldown:
                LOGGER.info("Suppressing duplicate email alert for key '%s' (cooldown active)", alert_key)
                return
        alert_cooldowns[alert_key] = now
        email_notifier.send(subject, message)

    store = TradeStore.from_env()
    LOGGER.info("Initializing paper-only Alpaca client and executor")
    broker_state_client = AlpacaBrokerStateClient.from_env()
    executor = AlpacaOrderExecutor.from_env()
    lifecycle = OrderLifecycleCoordinator(store, executor)
    fractional_protection = FractionalProtectionCoordinator(store, executor)
    if lifecycle is not None:
        retry_seconds = float(os.getenv("RECONCILIATION_RETRY_SECONDS", "30"))
        startup_reconciliation_alerted = False
        while True:
            try:
                lifecycle.reconcile_startup()
                if startup_reconciliation_alerted:
                    LOGGER.info("Startup reconciliation recovered; strategy loop may begin")
                break
            except OrderReconciliationRequired as exc:
                LOGGER.error("Startup remains fail-closed pending reconciliation: %s", exc)
                if not startup_reconciliation_alerted:
                    notifier.send(f"[critical] Startup remains fail-closed pending reconciliation: {exc}")
                    send_email_alert(
                        "startup_reconciliation",
                        "[Critical] Startup blocked pending reconciliation",
                        str(exc),
                    )
                    startup_reconciliation_alerted = True
                time.sleep(retry_seconds)
    loop_seconds = int(os.getenv("STRATEGY_LOOP_SECONDS", "10"))
    heartbeat = HeartbeatWriter(os.getenv("HEARTBEAT_PATH", "/app/data/heartbeat"))

    redis_url = os.getenv("REDIS_URL")
    redis_client = None
    if redis_url:
        try:
            redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
            redis_client.ping()
            LOGGER.info("Connected to Redis at %s", redis_url)
        except Exception as e:
            LOGGER.warning("Could not connect to Redis: %s. Market-hours gating will default to open.", e)
            redis_client = None

    LOGGER.info("Starting strategy loop for %s in lane %s", strategy.name, policy.name)
    heartbeat.start()

    try:
        while True:
            cycle_start = time.time()
            if lifecycle is not None:
                try:
                    lifecycle.reconcile_once()
                except OrderReconciliationRequired as exc:
                    LOGGER.error("Trading blocked pending order reconciliation: %s", exc)
                    notifier.send(f"[critical] Trading blocked pending order reconciliation: {exc}")
                    send_email_alert(
                        "continuous_reconciliation",
                        "[Critical] Trading blocked pending reconciliation",
                        str(exc),
                    )
                    time.sleep(loop_seconds)
                    continue
            try:
                broker_state = broker_state_client.fetch()
                broker_state_error = ""
            except BrokerStateError as exc:
                broker_state = None
                broker_state_error = str(exc)

            if broker_state is not None and fractional_protection is not None:
                try:
                    protection_failures = fractional_protection.reconcile(broker_state)
                except (FractionalProtectionError, OrderExecutionError) as exc:
                    protection_failures = [str(exc)]
                if protection_failures:
                    reason = "; ".join(protection_failures)
                    LOGGER.critical("Fractional protection unavailable; pausing live entries: %s", reason)
                    if redis_client is not None:
                        redis_client.set("bot_paused", "true")
                    send_email_alert(
                        "fractional_protection",
                        "[Critical] Fractional protection unavailable",
                        reason,
                    )
                    time.sleep(loop_seconds)
                    continue

            observation_log = getattr(getattr(strategy, "data", None), "observations", [])
            observation_log.clear()
            intent = strategy.run_cycle(broker_state)
            cycle_observations = list(observation_log)
            
            # Record cycle execution time
            cycle_duration = time.time() - cycle_start
            
            # Publish performance metrics to Redis
            if redis_client:
                try:
                    redis_client.incr("metrics:engine_cycles_total")
                    redis_client.set("metrics:engine_last_heartbeat", str(int(time.time())))
                    redis_client.set("metrics:engine_cycle_duration_seconds", f"{cycle_duration:.4f}")
                except Exception as e:
                    LOGGER.warning("Failed to publish cycle metrics to Redis: %s", e)

            LOGGER.info("Strategy cycle result: %s", intent)
            if intent.action in {"buy", "sell", "sell_short", "buy_to_cover", "sell_to_open", "sell_to_close", "buy_to_open", "buy_to_close"}:
                intent_id = deterministic_intent_id(intent)
                lane_block_reason = policy.explain_intent_block(intent.strategy, intent.action)
                if not lane_block_reason:
                    lane_block_reason = protected_symbol_block_reason(intent.symbol)
                if not lane_block_reason:
                    lane_block_reason = intent_data_block_reason(
                        intent, strategy, startup_settings, observations=cycle_observations
                    )
                
                # Check market-hours gating & user pause state via Redis
                if not lane_block_reason:
                    lane_block_reason = live_control_plane_block_reason(redis_client, startup_settings)
                    is_market_open = True
                    if not lane_block_reason and redis_client:
                        try:
                            paused_val = redis_client.get("bot_paused")
                            if paused_val == "true":
                                lane_block_reason = "service paused by user"
                            else:
                                val = redis_client.get("market_open")
                                if val == "false":
                                    is_market_open = False
                        except Exception as e:
                            LOGGER.warning("Failed to query market_open / bot_paused from Redis: %s", e)
                            if startup_settings.mode is OperatingMode.LIVE:
                                lane_block_reason = f"live control plane read failed: {type(e).__name__}"
                    
                    if not lane_block_reason and not is_market_open:
                        lane_block_reason = "market is closed"

                if lane_block_reason:
                    event = {
                        "intent_id": intent_id,
                        "strategy": intent.strategy,
                        "symbol": intent.symbol,
                        "action": intent.action,
                        "quantity": intent.quantity,
                        "order_value": intent.order_value,
                        "estimated_risk_value": intent.estimated_risk_value,
                        "account_nav_used": intent.account_nav,
                        "current_position_value_used": intent.current_position_value,
                        "state_source": "lane_policy",
                        "broker_state_error": "",
                        "allowed": False,
                        "reason": lane_block_reason,
                    }
                    store.log_trade_event(event)
                    notifier.send(
                        f"[{intent.strategy}] {intent.symbol} {intent.action} "
                        f"qty={intent.quantity} allowed=False reason={lane_block_reason} "
                        f"execution=lane_blocked"
                    )
                    send_email_alert(
                        f"blocked:{intent.symbol}:{intent.action}",
                        f"[Alert] Trade Blocked: {intent.symbol} {intent.action}",
                        f"Strategy: {intent.strategy}\nSymbol: {intent.symbol}\nAction: {intent.action}\nQuantity: {intent.quantity}\nOrder Value: ${intent.order_value:,.2f}\nReason: {lane_block_reason}\nExecution: lane_blocked"
                    )
                    time.sleep(loop_seconds)
                    continue

                account_nav_used = intent.account_nav
                current_position_value_used = intent.current_position_value
                state_source = "intent"

                if broker_state_error:
                    if broker_state_client.settings.require_for_trades:
                        allowed, reason = False, f"broker state unavailable: {broker_state_error}"
                        state_source = "broker_required_unavailable"
                    else:
                        allowed, reason = risk_manager.check_order(
                            symbol=intent.symbol,
                            quantity=intent.quantity,
                            order_value=intent.order_value,
                            account_nav=account_nav_used,
                            estimated_risk_value=intent.estimated_risk_value,
                            current_position_value=current_position_value_used,
                            action=intent.action,
                        )
                        state_source = "intent_fallback"
                else:
                    account_nav_used = broker_state.account_nav
                    current_position_value_used = broker_state.get_position_market_value(intent.symbol)
                    state_source = "alpaca"
                    if broker_state.trading_blocked:
                        allowed, reason = False, "alpaca account is trading_blocked"
                    else:
                        allowed, reason = risk_manager.check_order(
                            symbol=intent.symbol,
                            quantity=intent.quantity,
                            order_value=intent.order_value,
                            account_nav=account_nav_used,
                            estimated_risk_value=intent.estimated_risk_value,
                            current_position_value=current_position_value_used,
                            action=intent.action,
                            buying_power=broker_state.buying_power,
                            available_cash=broker_state.cash,
                            position_quantity=broker_state.get_position_quantity(intent.symbol),
                            position_count=len(broker_state.positions),
                            gross_exposure=broker_state.gross_exposure,
                            open_order_exposure=broker_state.open_order_exposure,
                            daily_pnl=broker_state.account_nav - broker_state.last_equity,
                            circuit_breaker_active=circuit_breaker_state(redis_client, risk_manager, broker_state),
                        )

                event = {
                    "intent_id": intent_id,
                    "strategy": intent.strategy,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "quantity": intent.quantity,
                    "order_value": intent.order_value,
                    "estimated_risk_value": intent.estimated_risk_value,
                    "account_nav_used": account_nav_used,
                    "current_position_value_used": current_position_value_used,
                    "state_source": state_source,
                    "broker_state_error": broker_state_error,
                    "allowed": allowed,
                    "reason": reason,
                }
                store.log_trade_event(event)
                execution_status = "blocked"
                if allowed:
                    try:
                        fractional_entry = (
                            fractional_protection is not None
                            and intent.action == "buy"
                            and is_fractional(intent.quantity)
                            and intent.stop_loss_price is not None
                            and intent.take_profit_price is not None
                        )
                        if fractional_entry:
                            fractional_protection.reserve_entry(intent, intent_id)
                        if (
                            fractional_protection is not None
                            and intent.action == "sell"
                            and is_fractional(intent.quantity)
                        ):
                            fractional_protection.cancel_stop_before_close(intent.symbol)
                        lifecycle_result = (
                            lifecycle.execute(intent, intent_id)
                            if lifecycle is not None
                            else None
                        )
                        execution_result = (
                            lifecycle_result.execution_result
                            if lifecycle_result is not None
                            else executor.execute(intent, intent_id=intent_id)
                        )
                        if fractional_entry and execution_result is not None:
                            fractional_protection.protect_entry_fill(
                                intent, intent_id, execution_result.broker_order_id,
                            )
                            execution_status = "submitted_fractional_protected"
                    except (OrderExecutionError, OrderReconciliationRequired, FractionalProtectionError) as exc:
                        execution_status = "executor_error"
                        if isinstance(exc, FractionalProtectionError) and redis_client is not None:
                            redis_client.set("bot_paused", "true")
                        send_email_alert(
                            f"execution_error:{intent.symbol}:{intent.action}",
                            f"[Alert] Execution Error: {intent.symbol} {intent.action}",
                            f"An error occurred while executing an order.\n\nStrategy: {intent.strategy}\nSymbol: {intent.symbol}\nAction: {intent.action}\nQuantity: {intent.quantity}\nError: {exc}"
                        )
                        store.log_order_event(
                            {
                                "intent_id": intent_id,
                                "strategy": intent.strategy,
                                "symbol": intent.symbol,
                                "action": intent.action,
                                "quantity": intent.quantity,
                                "requested_order_type": "market",
                                "requested_time_in_force": "unknown",
                                "dry_run": executor.settings.dry_run,
                                "status": "executor_error",
                                "broker": "alpaca",
                                "broker_order_id": "",
                                "request_payload": None,
                                "response_payload": None,
                                "error_message": str(exc),
                            }
                        )
                        if isinstance(exc, OrderReconciliationRequired):
                            raise
                    else:
                        if lifecycle_result is not None and lifecycle_result.status == "duplicate_blocked":
                            execution_status = "duplicate_blocked"
                            LOGGER.warning("Blocked duplicate order intent %s", intent_id)
                        else:
                            execution_status = (
                                "submitted_fractional_protected"
                                if fractional_entry else execution_result.status
                            )
                            store.log_order_event(execution_result.to_record())
                else:
                    send_email_alert(
                        f"blocked:{intent.symbol}:{intent.action}",
                        f"[Alert] Trade Blocked: {intent.symbol} {intent.action}",
                        f"Strategy: {intent.strategy}\nSymbol: {intent.symbol}\nAction: {intent.action}\nQuantity: {intent.quantity}\nOrder Value: ${intent.order_value:,.2f}\nReason: {reason}\nExecution: blocked"
                    )
                notifier.send(
                    f"[{intent.strategy}] {intent.symbol} {intent.action} "
                    f"qty={intent.quantity} allowed={allowed} reason={reason} "
                    f"execution={execution_status}"
                )
            time.sleep(loop_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Strategy loop interrupted, shutting down")
    except Exception as e:
        import traceback
        error_tb = traceback.format_exc()
        LOGGER.critical("Unhandled exception in strategy loop: %s", error_tb)
        email_notifier.send(
            subject=f"[Critical] Strategy Engine Crashed: {strategy.name}",
            body=f"The strategy engine encountered a critical unhandled exception and is shutting down.\n\nError: {e}\n\nStack Trace:\n{error_tb}"
        )
        raise e
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    main()
