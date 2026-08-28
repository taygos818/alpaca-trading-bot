"""Production paper-only entrypoint for the autonomous contest agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import logging
import os
import time

from agent_contracts import Direction
from ai_analysis import build_featherless_analysis_runtime
from defined_risk_options import (
    DefinedRiskOptionsConfig,
    DefinedRiskOptionsStrategy,
    DiscoveryCandidate,
    DynamicOptionsProposalBuilder,
    JsonlExitPlanStore,
    OptionsRiskState,
    normalize_alpaca_chain,
)
from execution_gateway import AlpacaCliGateway, GatewayPolicy, PaperCredentials
from external_data import (
    DataQualityEngine,
    FinnhubAdapter,
    FinnhubSettings,
    FredAdapter,
    FredSettings,
    ProviderUnavailable,
    YFinanceAdapter,
    YFinanceSettings,
)
from external_data.common import make_evidence, utc_datetime
from multi_agent import (
    AdversarialReviewAgent,
    AllocationLimits,
    DataQualityAgent,
    DeterministicAllocator,
    ExecutionAgent,
    OptionsStructureAgent,
    PortfolioRiskAgent,
    PortfolioSnapshot,
)
from paper_runtime import BoundedPaperLauncher, DecisionTraceJournal, PaperAgentCycleRunner, PaperLaunchPolicy
from utils.data_feed import DataFeedUnavailable, build_market_snapshot_provider
from utils.market_discovery import load_current_shortlist


LOGGER = logging.getLogger("contest-paper-agent")


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list_payload(payload, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [item for item in payload[key] if isinstance(item, dict)]
    return []


def _direction_and_rank(shortlist: dict, symbol: str) -> tuple[Direction, Decimal]:
    rows = [
        row
        for values in (shortlist.get("lanes") or {}).values()
        if isinstance(values, list)
        for row in values
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper()
    ]
    if not rows:
        raise ValueError("shortlist symbol has no ranked lane record")
    strongest = max(rows, key=lambda row: Decimal(str(row.get("activity_score") or 0)))
    return_pct = Decimal(str(strongest.get("return_pct") or 0))
    rank = max(Decimal(str(row.get("activity_score") or row.get("momentum_score") or row.get("pullback_score") or 0)) for row in rows)
    return (Direction.BULLISH if return_pct >= 0 else Direction.BEARISH), rank


class ContestPaperAgent:
    def __init__(self) -> None:
        self.config = DefinedRiskOptionsConfig.from_env()
        self.data = build_market_snapshot_provider()
        self.alpaca_data = getattr(self.data, "alpaca", None)
        if self.alpaca_data is None:
            raise RuntimeError("contest agent requires the Alpaca market-data provider")
        self.finnhub = FinnhubAdapter(FinnhubSettings.from_env())
        self.yfinance = YFinanceAdapter(YFinanceSettings.from_env())
        self.fred = FredAdapter(FredSettings.from_env())
        self.ai = build_featherless_analysis_runtime()
        submission = _flag("PAPER_ORDER_SUBMISSION_ENABLED")
        self.gateway = AlpacaCliGateway(
            self._credentials,
            policy=GatewayPolicy(shadow_mode=not submission, submission_enabled=submission),
        )
        self.launcher = BoundedPaperLauncher(self.gateway, PaperLaunchPolicy.from_env())
        self.journal = DecisionTraceJournal(os.getenv("DECISION_TRACE_PATH", "/app/logs/decision-traces.jsonl"))
        self.exit_plans = JsonlExitPlanStore(os.getenv("EXIT_PLAN_PATH", "/app/logs/exit-plans.jsonl"))

    @staticmethod
    def _credentials() -> PaperCredentials:
        return PaperCredentials(
            os.getenv("ALPACA_API_KEY", "").strip(),
            os.getenv("ALPACA_SECRET_KEY", "").strip(),
        )

    def run_once(self, now: datetime | None = None) -> tuple[object, ...]:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not _flag("AGENT_COORDINATOR_ENABLED") or not self.config.enabled:
            LOGGER.info("Contest agent entries are disabled")
            return ()
        clock = self.gateway.clock().payload
        if not isinstance(clock, dict) or not clock.get("is_open"):
            LOGGER.info("Market is closed; preserving reconciliation state without creating entries")
            return ()
        shortlist = load_current_shortlist(os.getenv("DISCOVERY_OUTPUT_PATH", "/app/paper-data/market-shortlist.json"), now=timestamp)
        account = self.gateway.account().payload
        positions = _list_payload(self.gateway.positions().payload, "positions")
        open_orders = _list_payload(self.gateway.open_orders().payload, "orders")
        risk_state, portfolio = self._portfolio(account, positions, open_orders)
        maximum = int(os.getenv("M6_MAX_CANDIDATES_PER_CYCLE", "5"))
        results = []
        for index, raw_symbol in enumerate(shortlist["symbols"][:maximum], start=1):
            symbol = str(raw_symbol).upper()
            trace_id = self._trace_id(symbol, timestamp)
            try:
                evidence, candidate = self._evidence_and_candidate(shortlist, symbol, trace_id, timestamp)
                if candidate is None:
                    continue
                chain_cache = {}

                def chain_provider(underlying, cycle_time):
                    if underlying not in chain_cache:
                        contracts = self.data.get_option_contracts(underlying)
                        snapshots = self.data.get_option_chain_snapshots(underlying)
                        chain_cache[underlying] = normalize_alpaca_chain(
                            underlying, contracts, snapshots, feed=self.config.required_feed
                        )
                    return chain_cache[underlying]

                builder = DynamicOptionsProposalBuilder(
                    DefinedRiskOptionsStrategy(self.config),
                    candidate_provider=lambda bundle, items, analyses, item=candidate: (item,),
                    chain_provider=chain_provider,
                    risk_provider=lambda item, state=risk_state: state,
                    clock=lambda current=timestamp: current,
                )
                limits = AllocationLimits(
                    max_open_positions=self.config.max_open_positions,
                    max_total_maximum_loss=risk_state.equity * self.config.max_total_risk_pct,
                    max_underlying_maximum_loss=risk_state.equity * self.config.max_underlying_risk_pct,
                )
                from multi_agent import MultiAgentCoordinator

                coordinator = MultiAgentCoordinator(
                    data_agent=DataQualityAgent(DataQualityEngine()),
                    analysis_agents=self.ai.agents,
                    structure_agents=(OptionsStructureAgent("defined_risk_options", builder),),
                    adversarial_agent=AdversarialReviewAgent(lambda proposal, bundle, items: ()),
                    risk_agent=PortfolioRiskAgent(DeterministicAllocator(limits)),
                    execution_agent=ExecutionAgent(),
                    preview_port=self.gateway,
                )
                result = PaperAgentCycleRunner(
                    coordinator,
                    self.launcher,
                    self.journal,
                    exit_plan_store=self.exit_plans,
                ).run_cycle(
                    trace_id=trace_id,
                    evidence=evidence,
                    portfolio=portfolio,
                    now=timestamp,
                    environment={"AGENT_COORDINATOR_ENABLED": "true", "AGENT_COORDINATOR_SHADOW_MODE": "true"},
                    display_metadata={
                        "opportunity_rankings": [{"symbol": symbol, "rank": index, "score": format(candidate.rank, "f")}]
                    },
                )
                results.append(result)
            except (DataFeedUnavailable, ProviderUnavailable, ValueError, RuntimeError) as exc:
                LOGGER.warning("Candidate %s failed closed: %s", symbol, type(exc).__name__)
        return tuple(results)

    def _evidence_and_candidate(self, shortlist, symbol, trace_id, now):
        bars = self.alpaca_data._get_bars(symbol, "1Min", 3)
        completed = [bar for bar in bars if bar.get("t") and utc_datetime(bar["t"]) + timedelta(minutes=1) <= now]
        if not completed:
            raise DataFeedUnavailable("no completed underlying bar")
        bar = completed[-1]
        bar_time = utc_datetime(bar["t"])
        bar_item = make_evidence(
            provider=f"alpaca_{self.alpaca_data.data_feed}", trace_id=trace_id, instrument=symbol,
            event_time=bar_time, received_at=now, value_name="completed_bar_close", payload=bar,
            source_uri="https://data.alpaca.markets/v2/stocks/bars", entitlement=self.alpaca_data.data_feed,
            is_fresh=now - bar_time <= timedelta(minutes=5), authority="broker_truth", session="regular",
            temporal_kind="observed", transformation_version="alpaca-completed-minute-bar-v1",
            numeric_value=Decimal(str(bar.get("c"))),
        )
        news = self.finnhub.company_news(
            symbol, (now - timedelta(days=3)).date(), now.date(), trace_id=trace_id, received_at=now
        )
        fresh_news = tuple(item for item in news if item.is_fresh)
        if not fresh_news:
            return (bar_item,), None
        research = []
        if YFinanceSettings.from_env().enabled:
            research.extend(self.yfinance.history(symbol, trace_id=trace_id, received_at=now)[-3:])
        if FredSettings.from_env().enabled:
            research.extend(self.fred.fetch("policy_rate", trace_id=trace_id, received_at=now))
        direction, rank = _direction_and_rank(shortlist, symbol)
        candidate = DiscoveryCandidate(
            symbol=symbol,
            direction=direction,
            catalyst_evidence_ids=tuple(item.record_id for item in fresh_news),
            completed_bar_evidence_ids=(bar_item.record_id,),
            correlation_group="broad_equity",
            rank=rank,
        )
        return tuple((bar_item, *fresh_news, *research)), candidate

    def _portfolio(self, account, positions, open_orders):
        if not isinstance(account, dict):
            raise RuntimeError("paper account state is unavailable")
        equity = Decimal(str(account.get("equity") or 0))
        cash = Decimal(str(account.get("cash") or 0))
        reserved = sum((abs(Decimal(str(item.get("cost_basis") or item.get("market_value") or 0))) for item in positions), Decimal("0"))
        open_underlyings = tuple(sorted({str(item.get("underlying_symbol") or item.get("symbol") or "") for item in positions if item.get("symbol")}))
        state = OptionsRiskState(
            options_trading_level=int(account.get("options_trading_level") or 0),
            equity=equity,
            cash=cash,
            options_buying_power=Decimal(str(account.get("options_buying_power") or account.get("buying_power") or 0)),
            daily_pnl=equity - Decimal(str(account.get("last_equity") or equity)),
            open_position_count=len(positions),
            pending_order_count=len(open_orders),
            reserved_maximum_loss=reserved,
            underlying_maximum_loss=Decimal("0"),
            correlation_maximum_loss=reserved,
        )
        portfolio = PortfolioSnapshot(open_underlyings, len(positions), reserved)
        return state, portfolio

    @staticmethod
    def _trace_id(symbol: str, now: datetime) -> str:
        digest = hashlib.sha256(f"{symbol}:{now.replace(second=0, microsecond=0).isoformat()}".encode()).hexdigest()[:20]
        return f"trace.contest.{digest}"


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    agent = ContestPaperAgent()
    loop_seconds = max(15, int(os.getenv("M6_AGENT_LOOP_SECONDS", "60")))
    while True:
        try:
            results = agent.run_once()
            LOGGER.info("Contest cycle completed traces=%d", len(results))
        except Exception as exc:
            LOGGER.exception("Contest cycle failed closed: %s", type(exc).__name__)
        time.sleep(loop_seconds)


if __name__ == "__main__":
    main()
