"""Credential-isolated factory for the coordinator's three analysis ports."""

from __future__ import annotations

from dataclasses import dataclass

from multi_agent import CatalystAgent, MacroAgent, TechnicalAgent

from .client import AuditSink, FeatherlessClient
from .models import FeatherlessSettings


@dataclass(frozen=True, slots=True)
class FeatherlessAnalysisRuntime:
    client: FeatherlessClient
    agents: tuple[TechnicalAgent, CatalystAgent, MacroAgent]


def build_featherless_analysis_runtime(
    settings: FeatherlessSettings | None = None,
    *,
    session=None,
    audit_sink: AuditSink | None = None,
) -> FeatherlessAnalysisRuntime:
    """Build research agents only; this factory has no risk, CLI, or broker dependency."""
    resolved = settings or FeatherlessSettings.from_env()
    client = FeatherlessClient(resolved, session=session, audit_sink=audit_sink)
    return FeatherlessAnalysisRuntime(
        client=client,
        agents=(
            TechnicalAgent(client.evaluator("technical")),
            CatalystAgent(client.evaluator("catalyst")),
            MacroAgent(client.evaluator("macro")),
        ),
    )
