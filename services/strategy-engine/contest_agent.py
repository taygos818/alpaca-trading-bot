"""Production paper-only entrypoint for the autonomous contest agent."""

from __future__ import annotations

from datetime import datetime, time as wall_time, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import hashlib
import logging
import os
import re
import time
from zoneinfo import ZoneInfo

from agent_contracts import ContractValidationError, Direction
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
from paper_runtime import (
    BoundedPaperLauncher,
    CompetitionPositionLifecycle,
    DecisionTraceJournal,
    ExitOrderStore,
    JsonlSubmissionLedger,
    PaperAgentCycleRunner,
    PaperLaunchPolicy,
    PendingEntryStore,
)
from utils.heartbeat import HeartbeatWriter
from utils.data_feed import DataFeedUnavailable, build_market_snapshot_provider
from utils.market_discovery import load_current_shortlist


LOGGER = logging.getLogger("contest-paper-agent")
OCC_OPTION_SYMBOL = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


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


def _ranked_record(shortlist: dict, symbol: str) -> dict:
    rows = [
        row
        for values in (shortlist.get("lanes") or {}).values()
        if isinstance(values, list)
        for row in values
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper()
    ]
    if not rows:
        raise ValueError("shortlist symbol has no ranked lane record")
    return max(
        rows,
        key=lambda row: Decimal(
            str(row.get("activity_score") or row.get("momentum_score") or row.get("pullback_score") or 0)
        ),
    )


def _eligible_contract_exists(contracts, now: datetime, config: DefinedRiskOptionsConfig) -> bool:
    for contract in contracts:
        try:
            expiration = datetime.fromisoformat(str(contract["expiration_date"])).date()
        except (KeyError, TypeError, ValueError):
            continue
        dte = (expiration - now.date()).days
        if (
            contract.get("tradable") is not False
            and str(contract.get("status", "active")).lower() == "active"
            and config.min_dte <= dte <= config.max_dte
        ):
            return True
    return False


def _deterministic_market_signal(bars, now: datetime):
    eastern = ZoneInfo("America/New_York")
    session_date = now.astimezone(eastern).date()
    completed = []
    for bar in bars:
        if not bar.get("t"):
            continue
        observed = utc_datetime(bar["t"])
        local = observed.astimezone(eastern)
        if (
            observed + timedelta(minutes=1) <= now
            and local.date() == session_date
            and wall_time(9, 30) <= local.time().replace(tzinfo=None) < wall_time(16, 0)
        ):
            completed.append((observed, bar))
    completed.sort(key=lambda row: row[0])
    if len(completed) < 6:
        return None
    values = [row[1] for row in completed]
    volumes = [Decimal(str(item.get("v") or 0)) for item in values]
    weighted = [
        ((Decimal(str(item.get("h") or item.get("c"))) + Decimal(str(item.get("l") or item.get("c"))) + Decimal(str(item.get("c")))) / Decimal("3")) * volume
        for item, volume in zip(values, volumes)
    ]
    total_volume = sum(volumes, Decimal("0"))
    if total_volume <= 0:
        return None
    vwap = sum(weighted, Decimal("0")) / total_volume
    closes = [Decimal(str(item.get("c") or 0)) for item in values]
    last = closes[-1]
    baseline_volumes = volumes[-21:-1] or volumes[:-1]
    average_volume = sum(baseline_volumes, Decimal("0")) / Decimal(len(baseline_volumes))
    if min(last, vwap, average_volume) <= 0:
        return None
    volume_ratio = volumes[-1] / average_volume
    threshold = Decimal(os.getenv("CONTEST_VWAP_CONFIRMATION_PCT", "0.001"))
    minimum_volume_ratio = Decimal(os.getenv("CONTEST_MIN_MINUTE_VOLUME_RATIO", "0.50"))
    rising = closes[-1] > closes[-3]
    falling = closes[-1] < closes[-3]
    direction = None
    if last >= vwap * (Decimal("1") + threshold) and rising and volume_ratio >= minimum_volume_ratio:
        direction = Direction.BULLISH
    elif last <= vwap * (Decimal("1") - threshold) and falling and volume_ratio >= minimum_volume_ratio:
        direction = Direction.BEARISH
    if direction is None:
        return None
    opening = values[: min(15, len(values))]
    return {
        "direction": direction,
        "last": last,
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "opening_high": max(Decimal(str(item.get("h") or item.get("c"))) for item in opening),
        "opening_low": min(Decimal(str(item.get("l") or item.get("c"))) for item in opening),
        "bar_time": completed[-1][0],
    }


def _bounded_fresh_news(news) -> tuple:
    maximum = max(1, int(os.getenv("M6_MAX_NEWS_ITEMS_PER_CANDIDATE", "8")))
    fresh = tuple(item for item in news if item.is_fresh)
    return fresh[-maximum:]


def _option_underlying(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    match = OCC_OPTION_SYMBOL.fullmatch(normalized)
    return match.group(1) if match else normalized


def _order_underlyings(order: dict) -> set[str]:
    symbols = {
        str(order.get("underlying_symbol") or ""),
        str(order.get("symbol") or ""),
    }
    legs = order.get("legs")
    if isinstance(legs, list):
        symbols.update(str(item.get("symbol") or "") for item in legs if isinstance(item, dict))
    return {_option_underlying(symbol) for symbol in symbols if symbol.strip()}


class ContestPaperAgent:
    def __init__(self) -> None:
        self.config = DefinedRiskOptionsConfig.from_env()
        self.data = build_market_snapshot_provider()
        self.alpaca_data = getattr(self.data, "alpaca", None)
        if self.alpaca_data is None:
            raise RuntimeError("contest agent requires the Alpaca market-data provider")
        self.finnhub = FinnhubAdapter(FinnhubSettings.from_env())
        self.yfinance = YFinanceAdapter(YFinanceSettings.from_env())
        self.ai = build_featherless_analysis_runtime()
        submission = _flag("PAPER_ORDER_SUBMISSION_ENABLED")
        self.gateway = AlpacaCliGateway(
            self._credentials,
            policy=GatewayPolicy(shadow_mode=not submission, submission_enabled=submission),
        )
        self.launcher = BoundedPaperLauncher(
            self.gateway,
            PaperLaunchPolicy.from_env(),
            JsonlSubmissionLedger(os.getenv("PAPER_SUBMISSION_LEDGER_PATH", "")),
        )
        self.journal = DecisionTraceJournal(os.getenv("DECISION_TRACE_PATH", "/app/logs/decision-traces.jsonl"))
        self.exit_plans = JsonlExitPlanStore(os.getenv("EXIT_PLAN_PATH", "/app/logs/exit-plans.jsonl"))
        self.pending_entries = PendingEntryStore(os.getenv("PENDING_ENTRY_PATH", "/app/logs/pending-entries.jsonl"))
        self.lifecycle = CompetitionPositionLifecycle(
            self.launcher,
            self.exit_plans,
            self.pending_entries,
            ExitOrderStore(os.getenv("EXIT_ORDER_PATH", "/app/logs/exit-orders.jsonl")),
        )
        self._optionability_cache: dict[str, tuple[datetime, tuple[dict, ...]]] = {}

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
        positions = _list_payload(self.gateway.positions().payload, "positions")
        reconciled = self.lifecycle.reconcile_entries()
        exits = self.lifecycle.manage_exits(
            positions,
            datetime.now(timezone.utc),
            quote_provider=self.data.get_option_chain_snapshots,
            thesis_provider=self._underlying_thesis_valid,
        )
        if reconciled or exits:
            LOGGER.info("Lifecycle reconciliation entries=%d exits=%d", len(reconciled), len(exits))
        entry_start = os.getenv("COMPETITION_ENTRY_START_AT", "").strip()
        entry_cutoff = os.getenv("COMPETITION_ENTRY_CUTOFF_AT", "").strip()
        if entry_start and timestamp < datetime.fromisoformat(entry_start).astimezone(timezone.utc):
            LOGGER.info("Competition entry window has not opened")
            return ()
        if entry_cutoff and timestamp >= datetime.fromisoformat(entry_cutoff).astimezone(timezone.utc):
            LOGGER.info("Competition entry cutoff reached; exits remain active")
            return ()
        shortlist = load_current_shortlist(os.getenv("DISCOVERY_OUTPUT_PATH", "/app/paper-data/market-shortlist.json"), now=timestamp)
        account = self.gateway.account().payload
        open_orders = _list_payload(self.gateway.open_orders().payload, "orders")
        risk_state, portfolio = self._portfolio(account, positions, open_orders)
        occupied_underlyings = set(portfolio.open_underlyings)
        occupied_underlyings.update(
            underlying
            for order in open_orders
            for underlying in _order_underlyings(order)
        )
        underlying_risk = dict(portfolio.underlying_maximum_loss)
        maximum = int(os.getenv("M6_MAX_CANDIDATES_PER_CYCLE", "5"))
        results = []
        evaluated = 0
        for index, raw_symbol in enumerate(shortlist["symbols"], start=1):
            if evaluated >= maximum:
                break
            symbol = str(raw_symbol).upper()
            trace_id = self._trace_id(symbol, timestamp)
            if symbol in occupied_underlyings:
                LOGGER.info("Candidate %s suppressed because the underlying already has exposure or an open order", symbol)
                self.journal.append_failure(
                    trace_id=trace_id,
                    phase="portfolio_gate",
                    outcome="existing_underlying_exposure",
                    metadata={"opportunity_rankings": [{"symbol": symbol, "rank": index}]},
                    recorded_at=timestamp,
                )
                continue
            try:
                candidate_time = datetime.now(timezone.utc)
                contracts = self._option_contracts(symbol, candidate_time)
                if not _eligible_contract_exists(contracts, candidate_time, self.config):
                    self.journal.append_failure(
                        trace_id=trace_id,
                        phase="optionability_gate",
                        outcome="not_options_eligible",
                        metadata={"opportunity_rankings": [{"symbol": symbol, "rank": index}]},
                        recorded_at=candidate_time,
                    )
                    continue
                evaluated += 1
                evidence, candidate, provider_failures = self._evidence_and_candidate(
                    shortlist, symbol, trace_id, candidate_time
                )
                if candidate is None:
                    continue
                chain_cache = {}

                def chain_provider(underlying, cycle_time):
                    if underlying not in chain_cache:
                        contracts = self._option_contracts(underlying, datetime.now(timezone.utc))
                        snapshots = self.data.get_option_chain_snapshots(underlying)
                        chain_cache[underlying] = normalize_alpaca_chain(
                            underlying, contracts, snapshots, feed=self.config.required_feed
                        )
                    return chain_cache[underlying]

                builder = DynamicOptionsProposalBuilder(
                    DefinedRiskOptionsStrategy(self.config),
                    candidate_provider=lambda bundle, items, analyses, item=candidate: (item,),
                    chain_provider=chain_provider,
                    risk_provider=lambda item, state=risk_state, by_symbol=underlying_risk: replace(
                        state,
                        underlying_maximum_loss=by_symbol.get(item.symbol, Decimal("0")),
                    ),
                    # Option snapshots are fetched after evidence and AI analysis.
                    # Use selection time for quote age so newly arrived quotes are
                    # not rejected as being in the future relative to cycle start.
                    clock=lambda: datetime.now(timezone.utc),
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
                    pending_entry_store=self.pending_entries,
                ).run_cycle(
                    trace_id=trace_id,
                    evidence=evidence,
                    portfolio=portfolio,
                    now=candidate_time,
                    environment={"AGENT_COORDINATOR_ENABLED": "true", "AGENT_COORDINATOR_SHADOW_MODE": "true"},
                    display_metadata={
                        "opportunity_rankings": [{"symbol": symbol, "rank": index, "score": format(candidate.rank, "f")}],
                        "provider_failures": provider_failures,
                    },
                )
                results.append(result)
            except (DataFeedUnavailable, ProviderUnavailable, ValueError, RuntimeError) as exc:
                LOGGER.warning("Candidate %s failed closed: %s", symbol, type(exc).__name__)
                self.journal.append_failure(
                    trace_id=trace_id,
                    phase="evidence_or_analysis",
                    outcome="provider_unavailable" if isinstance(exc, (DataFeedUnavailable, ProviderUnavailable)) else "cycle_failed",
                    metadata={
                        "opportunity_rankings": [{"symbol": symbol, "rank": index}],
                        "provider_failures": [{
                            "provider": "candidate_pipeline",
                            "error_type": type(exc).__name__,
                            "reason": (
                                str(exc)[:240]
                                if isinstance(exc, ContractValidationError)
                                else "required candidate evidence or analysis was unavailable"
                            ),
                        }],
                    },
                    recorded_at=datetime.now(timezone.utc),
                )
        return tuple(results)

    def _option_contracts(self, symbol: str, now: datetime) -> tuple[dict, ...]:
        cached = self._optionability_cache.get(symbol)
        if cached is not None and now < cached[0]:
            return cached[1]
        contracts = tuple(self.data.get_option_contracts(symbol))
        ttl = max(60, int(os.getenv("CONTEST_OPTIONABILITY_CACHE_SECONDS", "900")))
        self._optionability_cache[symbol] = (now + timedelta(seconds=ttl), contracts)
        return contracts

    def _evidence_and_candidate(self, shortlist, symbol, trace_id, now):
        bars = self.alpaca_data._get_bars(symbol, "1Min", 390)
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
        signal = _deterministic_market_signal(completed, now)
        if signal is None:
            return (bar_item,), None, []
        signal_payload = {
            "direction": signal["direction"].value,
            "last": format(signal["last"], "f"),
            "session_vwap": format(signal["vwap"], "f"),
            "minute_volume_ratio": format(signal["volume_ratio"], "f"),
            "opening_high": format(signal["opening_high"], "f"),
            "opening_low": format(signal["opening_low"], "f"),
        }
        signal_item = make_evidence(
            provider=f"alpaca_{self.alpaca_data.data_feed}", trace_id=trace_id, instrument=symbol,
            event_time=signal["bar_time"], received_at=now, value_name="deterministic_vwap_signal",
            payload=signal_payload, source_uri="https://data.alpaca.markets/v2/stocks/bars",
            entitlement=self.alpaca_data.data_feed, is_fresh=now - signal["bar_time"] <= timedelta(minutes=5),
            authority="broker_truth", session="regular", temporal_kind="observed",
            transformation_version="completed-bars-vwap-trend-volume-v1", numeric_value=signal["last"],
        )
        ranked = _ranked_record(shortlist, symbol)
        _, rank = _direction_and_rank(shortlist, symbol)
        activity_item = make_evidence(
            provider="alpaca_sip_screener", trace_id=trace_id, instrument=symbol,
            event_time=now, received_at=now, value_name="ranked_market_activity",
            payload={
                "return_pct": ranked.get("return_pct"),
                "relative_volume": ranked.get("relative_volume"),
                "activity_score": ranked.get("activity_score"),
                "momentum_score": ranked.get("momentum_score"),
                "rank": format(rank, "f"),
            },
            source_uri="https://data.alpaca.markets/v1beta1/screener/stocks",
            entitlement="sip", is_fresh=True, authority="broker_truth", session="regular",
            temporal_kind="observed", transformation_version="ranked-market-activity-v1",
            numeric_value=rank,
        )
        news = self.finnhub.company_news(
            symbol, (now - timedelta(days=3)).date(), now.date(), trace_id=trace_id, received_at=now
        )
        fresh_news = _bounded_fresh_news(news)
        research, provider_failures = self._optional_research(symbol, trace_id, now)
        candidate = DiscoveryCandidate(
            symbol=symbol,
            direction=signal["direction"],
            catalyst_evidence_ids=(activity_item.record_id, *(item.record_id for item in fresh_news)),
            completed_bar_evidence_ids=(bar_item.record_id, signal_item.record_id),
            correlation_group="broad_equity",
            rank=rank,
        )
        return tuple((bar_item, signal_item, activity_item, *fresh_news, *research)), candidate, provider_failures

    def _underlying_thesis_valid(self, plan, now: datetime) -> bool:
        bars = self.alpaca_data._get_bars(plan.underlying, "1Min", 390)
        signal = _deterministic_market_signal(bars, now)
        if signal is None:
            return True
        expected = Direction.BULLISH if plan.legs[0].right.value == "call" else Direction.BEARISH
        return signal["direction"] is expected

    def _optional_research(self, symbol, trace_id, now):
        research = []
        failures = []
        providers = (
            (
                "yfinance",
                YFinanceSettings.from_env().enabled,
                lambda: self.yfinance.history(symbol, trace_id=trace_id, received_at=now)[-3:],
            ),
        )
        for provider, enabled, fetch in providers:
            if not enabled:
                continue
            try:
                research.extend(fetch())
            except ProviderUnavailable as exc:
                LOGGER.warning(
                    "Candidate %s optional provider %s degraded: %s",
                    symbol,
                    provider,
                    type(exc).__name__,
                )
                failures.append({
                    "provider": provider,
                    "error_type": type(exc).__name__,
                    "reason": "optional secondary research unavailable",
                })
        return research, failures

    def _portfolio(self, account, positions, open_orders):
        if not isinstance(account, dict):
            raise RuntimeError("paper account state is unavailable")
        equity = Decimal(str(account.get("equity") or 0))
        cash = Decimal(str(account.get("cash") or 0))
        risk_by_underlying: dict[str, Decimal] = {}
        for item in positions:
            symbol = _option_underlying(str(item.get("underlying_symbol") or item.get("symbol") or ""))
            if not symbol:
                continue
            amount = abs(Decimal(str(item.get("cost_basis") or item.get("market_value") or 0)))
            risk_by_underlying[symbol] = risk_by_underlying.get(symbol, Decimal("0")) + amount
        reserved = sum(risk_by_underlying.values(), Decimal("0"))
        open_underlyings = tuple(sorted(risk_by_underlying))
        state = OptionsRiskState(
            options_trading_level=int(account.get("options_trading_level") or 0),
            equity=equity,
            cash=cash,
            options_buying_power=Decimal(str(account.get("options_buying_power") or account.get("buying_power") or 0)),
            daily_pnl=equity - Decimal(str(account.get("last_equity") or equity)),
            open_position_count=len(open_underlyings),
            pending_order_count=len(open_orders),
            reserved_maximum_loss=reserved,
            underlying_maximum_loss=Decimal("0"),
            correlation_maximum_loss=reserved,
        )
        portfolio = PortfolioSnapshot(
            open_underlyings,
            len(open_underlyings),
            reserved,
            tuple(sorted(risk_by_underlying.items())),
        )
        return state, portfolio

    @staticmethod
    def _trace_id(symbol: str, now: datetime) -> str:
        digest = hashlib.sha256(f"{symbol}:{now.replace(second=0, microsecond=0).isoformat()}".encode()).hexdigest()[:20]
        return f"trace.contest.{digest}"


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    heartbeat = HeartbeatWriter(os.getenv("HEARTBEAT_PATH", "/app/paper-data/engine-heartbeat"))
    heartbeat.start()
    agent = ContestPaperAgent()
    loop_seconds = max(15, int(os.getenv("M6_AGENT_LOOP_SECONDS", "60")))
    try:
        while True:
            try:
                results = agent.run_once()
                LOGGER.info("Contest cycle completed traces=%d", len(results))
            except Exception as exc:
                LOGGER.exception("Contest cycle failed closed: %s", type(exc).__name__)
            time.sleep(loop_seconds)
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    main()
