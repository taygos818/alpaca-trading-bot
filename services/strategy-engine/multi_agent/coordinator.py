"""Concurrent proposal coordination with deterministic ordering and shadow-only output."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import os
import threading
from typing import Protocol

from agent_contracts import (
    AdversarialObjection,
    AgentAnalysis,
    AuthorizedExecution,
    ContractValidationError,
    EvidenceBundle,
    EvidenceItem,
    ExecutionCommand,
    OptionsProposal,
    RiskAuthorization,
    RiskDecision,
    canonical_json,
    contract_fingerprint,
    coordinator_contracts_enabled,
)

from .agents import (
    AdversarialReviewAgent,
    DataQualityAgent,
    EvidenceAnalysisAgent,
    ExecutionAgent,
    OptionsStructureAgent,
)
from .allocator import PortfolioRiskAgent
from .models import PortfolioSnapshot


class CoordinatorDisabled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoordinatorPolicy:
    shadow_mode: bool = True
    max_workers: int = 6

    def __post_init__(self) -> None:
        if not self.shadow_mode:
            raise ValueError("Milestone 2 coordinator is shadow-only")
        if self.max_workers <= 0 or self.max_workers > 16:
            raise ValueError("max_workers must be between 1 and 16")


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    bundle: EvidenceBundle
    analyses: tuple[AgentAnalysis, ...]
    proposals: tuple[OptionsProposal, ...]
    objections: tuple[AdversarialObjection, ...]
    authorizations: tuple[RiskAuthorization, ...]
    commands: tuple[ExecutionCommand, ...]
    authorized_executions: tuple[AuthorizedExecution, ...]
    previews: tuple[str, ...]
    duplicate_proposal_ids: tuple[str, ...]


class IdempotencyRegistry:
    """Detect identical retries and conflicting reuse of stable proposal IDs."""

    def __init__(self) -> None:
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.Lock()

    def claim(self, proposal: OptionsProposal) -> bool:
        fingerprint = contract_fingerprint(proposal)
        with self._lock:
            existing = self._fingerprints.get(proposal.record_id)
            if existing is None:
                self._fingerprints[proposal.record_id] = fingerprint
                return True
            if existing != fingerprint:
                raise ContractValidationError("stable proposal ID was reused with different content")
            return False


class ShadowPreviewPort(Protocol):
    def preview(self, execution: AuthorizedExecution) -> object: ...


class MultiAgentCoordinator:
    """Runs independent analysis/proposal agents and emits previews only."""

    def __init__(
        self,
        *,
        data_agent: DataQualityAgent,
        analysis_agents: tuple[EvidenceAnalysisAgent, ...],
        structure_agents: tuple[OptionsStructureAgent, ...],
        adversarial_agent: AdversarialReviewAgent,
        risk_agent: PortfolioRiskAgent,
        execution_agent: ExecutionAgent,
        preview_port: ShadowPreviewPort,
        registry: IdempotencyRegistry | None = None,
        policy: CoordinatorPolicy | None = None,
    ) -> None:
        if not analysis_agents or not structure_agents:
            raise ValueError("coordinator requires analysis and structure agents")
        names = [agent.name for agent in (*analysis_agents, *structure_agents)]
        if len(names) != len(set(names)):
            raise ValueError("agent names must be unique")
        self._data_agent = data_agent
        self._analysis_agents = analysis_agents
        self._structure_agents = structure_agents
        self._adversarial_agent = adversarial_agent
        self._risk_agent = risk_agent
        self._execution_agent = execution_agent
        if not callable(getattr(preview_port, "preview", None)):
            raise ValueError("coordinator requires a preview-only port")
        self._preview_port = preview_port
        self._registry = registry or IdempotencyRegistry()
        self._policy = policy or CoordinatorPolicy()

    def run_shadow_cycle(
        self,
        *,
        trace_id: str,
        evidence: tuple[EvidenceItem, ...],
        portfolio: PortfolioSnapshot,
        now: datetime,
        environment: dict[str, str] | None = None,
    ) -> CoordinatorResult:
        if not coordinator_contracts_enabled(environment):
            raise CoordinatorDisabled("AGENT_COORDINATOR_ENABLED is false")
        source = os.environ if environment is None else environment
        if source.get("AGENT_COORDINATOR_SHADOW_MODE", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            raise CoordinatorDisabled("AGENT_COORDINATOR_SHADOW_MODE must remain true")
        bundle = self._data_agent.freeze(trace_id, evidence, now)

        with ThreadPoolExecutor(max_workers=min(self._policy.max_workers, len(self._analysis_agents))) as pool:
            analysis_futures = [
                pool.submit(agent.analyze, bundle, evidence, now)
                for agent in self._analysis_agents
            ]
            analyses = tuple(
                sorted(
                    (future.result() for future in analysis_futures),
                    key=lambda item: item.record_id,
                )
            )

        with ThreadPoolExecutor(max_workers=min(self._policy.max_workers, len(self._structure_agents))) as pool:
            proposal_futures = [
                pool.submit(agent.propose, bundle, evidence, analyses, now)
                for agent in self._structure_agents
            ]
            proposed = tuple(
                proposal
                for future in proposal_futures
                for proposal in future.result()
            )

        proposals, in_cycle_duplicates = self._deduplicate(proposed)
        claimed = []
        duplicate_ids = list(in_cycle_duplicates)
        for proposal in proposals:
            if self._registry.claim(proposal):
                claimed.append(proposal)
            else:
                duplicate_ids.append(proposal.record_id)

        objections = tuple(
            sorted(
                (
                    objection
                    for proposal in claimed
                    for objection in self._adversarial_agent.review(proposal, bundle, evidence, now)
                ),
                key=lambda item: item.record_id,
            )
        )
        authorizations = self._risk_agent.authorize(tuple(claimed), objections, portfolio, now)
        proposal_by_id = {proposal.record_id: proposal for proposal in claimed}
        commands = tuple(
            self._execution_agent.command(proposal_by_id[item.proposal_id], item, now)
            for item in authorizations
            if item.decision is not RiskDecision.REJECT
        )
        authorization_by_proposal = {item.proposal_id: item for item in authorizations}
        authorized_executions = tuple(
            AuthorizedExecution(
                proposal=proposal_by_id[command.proposal_id],
                authorization=authorization_by_proposal[command.proposal_id],
                command=command,
            )
            for command in commands
        )
        previews = tuple(canonical_json(self._preview_port.preview(execution)) for execution in authorized_executions)
        return CoordinatorResult(
            bundle=bundle,
            analyses=analyses,
            proposals=tuple(claimed),
            objections=objections,
            authorizations=authorizations,
            commands=commands,
            authorized_executions=authorized_executions,
            previews=previews,
            duplicate_proposal_ids=tuple(sorted(set(duplicate_ids))),
        )

    @staticmethod
    def _deduplicate(proposals: tuple[OptionsProposal, ...]) -> tuple[tuple[OptionsProposal, ...], tuple[str, ...]]:
        by_id: dict[str, OptionsProposal] = {}
        duplicates = []
        for proposal in proposals:
            existing = by_id.get(proposal.record_id)
            if existing is None:
                by_id[proposal.record_id] = proposal
            elif contract_fingerprint(existing) == contract_fingerprint(proposal):
                duplicates.append(proposal.record_id)
            else:
                raise ContractValidationError("agents produced conflicting content for one proposal ID")
        return tuple(sorted(by_id.values(), key=lambda item: item.record_id)), tuple(sorted(set(duplicates)))
