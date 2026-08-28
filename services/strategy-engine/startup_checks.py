import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psycopg
import redis

from config_loader import StrategyConfigError, load_strategy_config
from trading_lane import TradingLanePolicy
from utils.alpaca_credentials import alpaca_credentials


LIVE_ACKNOWLEDGEMENT = "I_UNDERSTAND_LIVE_CAPITAL_IS_AT_RISK"
LIVE_TRADING_API_URL = "https://api.alpaca.markets"
PAPER_TRADING_API_URL = "https://paper-api.alpaca.markets"
PROJECT_PAPER_ONLY = True


class StartupReadinessError(RuntimeError):
    pass


class OperatingMode(str, Enum):
    OFFLINE = "offline"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _mode_from_env() -> OperatingMode:
    raw = os.getenv("BOT_ENVIRONMENT", "paper").strip().lower()
    try:
        return OperatingMode(raw)
    except ValueError as exc:
        raise StartupReadinessError(f"Unsupported BOT_ENVIRONMENT: {raw}") from exc


@dataclass(frozen=True)
class StartupSettings:
    trading_lane: str
    bot_strategy: str
    alpaca_paper_trade: bool
    alpaca_order_dry_run: bool
    require_broker_state_for_trades: bool
    paper_order_submission_enabled: bool
    alpaca_api_key: str
    alpaca_secret_key: str
    active_broker: str = "alpaca"
    mode: OperatingMode = OperatingMode.PAPER
    trading_api_url: str = PAPER_TRADING_API_URL
    market_data_provider: str = "alpaca"
    allow_mock_data_fallback: bool = False
    allow_mock_iv_rank: bool = False
    live_trading_enabled: bool = False
    live_acknowledgement: str = ""
    redis_url: str = ""
    postgres_dsn: str = ""
    heartbeat_path: str = "/app/data/heartbeat"
    strategy_config_path: str = "strategy.toml"
    discord_webhook_url: str = ""
    smtp_host: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    max_trade_risk_pct: float = 0.01
    daily_drawdown_limit: float = 0.03
    max_concentration_pct: float = 0.15
    control_state_max_age_seconds: float = 90.0
    max_quote_deviation_pct: float = 0.01
    max_quote_age_seconds: float = 15.0

    @classmethod
    def from_env(cls):
        paper_trade = _parse_bool(os.getenv("ALPACA_PAPER_TRADE"), True)
        api_key, secret_key = alpaca_credentials(paper_trade)
        default_url = PAPER_TRADING_API_URL if paper_trade else LIVE_TRADING_API_URL
        return cls(
            mode=_mode_from_env(),
            trading_lane=os.getenv("TRADING_LANE", "stock_paper").strip().lower(),
            bot_strategy=os.getenv("BOT_STRATEGY", "tier2_swing").strip(),
            alpaca_paper_trade=paper_trade,
            alpaca_order_dry_run=_parse_bool(os.getenv("ALPACA_ORDER_DRY_RUN"), True),
            require_broker_state_for_trades=_parse_bool(os.getenv("REQUIRE_BROKER_STATE_FOR_TRADES"), True),
            paper_order_submission_enabled=_parse_bool(os.getenv("PAPER_ORDER_SUBMISSION_ENABLED"), False),
            alpaca_api_key=api_key,
            alpaca_secret_key=secret_key,
            active_broker=os.getenv("ACTIVE_BROKER", "alpaca").strip().lower(),
            trading_api_url=os.getenv("ALPACA_TRADING_API_URL", default_url).rstrip("/"),
            market_data_provider=os.getenv("MARKET_DATA_PROVIDER", "alpaca").strip().lower(),
            allow_mock_data_fallback=_parse_bool(os.getenv("ALLOW_MOCK_DATA_FALLBACK"), False),
            allow_mock_iv_rank=_parse_bool(os.getenv("ALLOW_MOCK_IV_RANK"), False),
            live_trading_enabled=_parse_bool(os.getenv("LIVE_TRADING_ENABLED"), False),
            live_acknowledgement=os.getenv("LIVE_TRADING_ACKNOWLEDGEMENT", "").strip(),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            postgres_dsn=os.getenv("POSTGRES_DSN", "").strip(),
            heartbeat_path=os.getenv("HEARTBEAT_PATH", "/app/data/heartbeat").strip(),
            strategy_config_path=os.getenv("STRATEGY_CONFIG_PATH", "strategy.toml").strip(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_to=os.getenv("SMTP_TO", "").strip(),
            max_trade_risk_pct=float(os.getenv("MAX_TRADE_RISK_PCT", "0.01")),
            daily_drawdown_limit=float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.03")),
            max_concentration_pct=float(os.getenv("MAX_CONCENTRATION_PCT", "0.15")),
            control_state_max_age_seconds=float(os.getenv("CONTROL_STATE_MAX_AGE_SECONDS", "90")),
            max_quote_deviation_pct=float(os.getenv("MAX_QUOTE_DEVIATION_PCT", "0.01")),
            max_quote_age_seconds=float(os.getenv("MAX_QUOTE_AGE_SECONDS", "15")),
        )


def _validate_strategy_config(path: str, failures: list[str]):
    try:
        load_strategy_config(path)
    except StrategyConfigError as exc:
        failures.append(str(exc))


def validate_startup_readiness(
    settings: StartupSettings | None = None,
    policy: TradingLanePolicy | None = None,
) -> StartupSettings:
    resolved = settings or StartupSettings.from_env()
    resolved_policy = policy or TradingLanePolicy.from_env()
    failures: list[str] = []

    if resolved.trading_lane != resolved_policy.name:
        failures.append(f"TRADING_LANE={resolved.trading_lane} does not match active policy {resolved_policy.name}")
    if PROJECT_PAPER_ONLY and resolved.mode is OperatingMode.LIVE:
        failures.append("This project is paper-only and permanently rejects BOT_ENVIRONMENT=live")
    if resolved.active_broker != "alpaca":
        failures.append("This project supports only ACTIVE_BROKER=alpaca")
    if resolved.mode in {OperatingMode.PAPER, OperatingMode.LIVE} and not resolved_policy.supports_strategy(resolved.bot_strategy):
        failures.append(f"BOT_STRATEGY={resolved.bot_strategy} is not enabled in TRADING_LANE={resolved_policy.name}")

    if resolved.mode in {OperatingMode.OFFLINE, OperatingMode.BACKTEST}:
        if resolved_policy.name != "disabled":
            failures.append(f"BOT_ENVIRONMENT={resolved.mode.value} requires TRADING_LANE=disabled")
        if not resolved.alpaca_order_dry_run:
            failures.append(f"BOT_ENVIRONMENT={resolved.mode.value} requires ALPACA_ORDER_DRY_RUN=true")
    elif resolved.mode is OperatingMode.PAPER:
        if "paper" not in resolved_policy.name:
            failures.append("BOT_ENVIRONMENT=paper requires a paper trading lane")
        if not resolved.alpaca_paper_trade or resolved.trading_api_url != PAPER_TRADING_API_URL:
            failures.append("Paper mode requires paper credentials and https://paper-api.alpaca.markets")
        if not resolved.alpaca_order_dry_run and not resolved.paper_order_submission_enabled:
            failures.append("Set PAPER_ORDER_SUBMISSION_ENABLED=true before submitting paper orders")
    elif resolved.mode is OperatingMode.LIVE:
        failures.append("Live trading configuration is not supported by this contest project")

    if not resolved.require_broker_state_for_trades:
        failures.append("REQUIRE_BROKER_STATE_FOR_TRADES must remain true")
    if resolved.mode in {OperatingMode.PAPER, OperatingMode.LIVE} and (
        not resolved.alpaca_api_key or not resolved.alpaca_secret_key
    ):
        failures.append("ALPACA_API_KEY and ALPACA_SECRET_KEY must be configured")

    limits = (
        ("MAX_TRADE_RISK_PCT", resolved.max_trade_risk_pct, 0.01),
        ("DAILY_DRAWDOWN_LIMIT", resolved.daily_drawdown_limit, 0.03),
        ("MAX_CONCENTRATION_PCT", resolved.max_concentration_pct, 0.15),
    )
    for name, value, live_maximum in limits:
        if value <= 0 or value > 1:
            failures.append(f"{name} must be greater than 0 and no greater than 1")
        if resolved.mode is OperatingMode.LIVE and value > live_maximum:
            failures.append(f"{name} exceeds the approved live maximum {live_maximum}")

    _validate_strategy_config(resolved.strategy_config_path, failures)
    if failures:
        raise StartupReadinessError(" | ".join(failures))
    return resolved


def validate_runtime_dependencies(settings: StartupSettings, now: float | None = None):
    if settings.mode is not OperatingMode.LIVE:
        return

    failures: list[str] = []
    current_time = time.time() if now is None else now

    try:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
        client.ping()
        market_open = client.get("market_open")
        updated_at = client.get("market_open_updated_at")
        paused = client.get("bot_paused")
        if market_open not in {"true", "false"}:
            failures.append("Redis market_open state is missing or invalid")
        try:
            age = current_time - float(updated_at)
            if age < 0 or age > settings.control_state_max_age_seconds:
                failures.append("Redis market clock state is stale")
        except (TypeError, ValueError):
            failures.append("Redis market clock timestamp is missing or invalid")
        if paused not in {"true", "false"}:
            failures.append("Redis bot_paused state is missing or invalid")
    except Exception as exc:
        failures.append(f"Redis readiness failed: {type(exc).__name__}")

    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=3) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE startup_write_probe (value INTEGER)")
                cursor.execute("INSERT INTO startup_write_probe (value) VALUES (1)")
    except Exception as exc:
        failures.append(f"PostgreSQL write readiness failed: {type(exc).__name__}")

    try:
        heartbeat_parent = Path(settings.heartbeat_path).parent
        heartbeat_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=heartbeat_parent, prefix=".startup-probe-", delete=True):
            pass
    except Exception as exc:
        failures.append(f"Heartbeat path readiness failed: {type(exc).__name__}")

    if failures:
        raise StartupReadinessError(" | ".join(failures))
