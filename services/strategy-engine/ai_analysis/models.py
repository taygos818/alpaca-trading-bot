"""Pinned Featherless configuration and immutable inference audit records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import os


PROMPT_VERSION = "featherless-evidence-analysis-2026-08-28-v1"
MODEL_REGISTRY_VERSION = "featherless-models-2026-08-28-v1"
DEFAULT_MODEL_ID = "Qwen/Qwen3-30B-A3B-Instruct-2507"


class FeatherlessError(RuntimeError):
    """Base class for fail-closed inference errors."""


class FeatherlessDisabled(FeatherlessError):
    pass


class FeatherlessUnavailable(FeatherlessError):
    pass


class FeatherlessRateLimited(FeatherlessUnavailable):
    pass


class FeatherlessInvalidOutput(FeatherlessError):
    pass


class FeatherlessBudgetExceeded(FeatherlessError):
    pass


@dataclass(frozen=True, slots=True)
class FeatherlessModelProfile:
    model_id: str
    context_length: int
    max_completion_tokens: int
    prompt_price_per_token_usd: Decimal
    completion_price_per_token_usd: Decimal
    concurrency_cost: int
    catalog_snapshot: str = "2026-08-28"

    def __post_init__(self) -> None:
        if not self.model_id or self.context_length <= 0 or self.max_completion_tokens <= 0:
            raise ValueError("invalid Featherless model profile")
        if self.prompt_price_per_token_usd < 0 or self.completion_price_per_token_usd < 0:
            raise ValueError("model pricing cannot be negative")
        if self.concurrency_cost <= 0:
            raise ValueError("model concurrency cost must be positive")


FEATHERLESS_MODEL_REGISTRY = {
    DEFAULT_MODEL_ID: FeatherlessModelProfile(
        model_id=DEFAULT_MODEL_ID,
        context_length=32768,
        max_completion_tokens=32768,
        prompt_price_per_token_usd=Decimal("0.000000125"),
        completion_price_per_token_usd=Decimal("0.00000045"),
        concurrency_cost=1,
    ),
    "Qwen/Qwen3-14B": FeatherlessModelProfile(
        model_id="Qwen/Qwen3-14B",
        context_length=32768,
        max_completion_tokens=32768,
        prompt_price_per_token_usd=Decimal("0.00000012"),
        completion_price_per_token_usd=Decimal("0.00000024"),
        concurrency_cost=1,
    ),
}


@dataclass(frozen=True, slots=True)
class FeatherlessSettings:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.featherless.ai/v1"
    model_id: str = DEFAULT_MODEL_ID
    timeout_seconds: float = 30.0
    max_completion_tokens: int = 700
    max_prompt_chars: int = 24000
    max_requests_per_trace: int = 3
    max_total_tokens_per_trace: int = 75000
    max_cost_per_trace_usd: Decimal = Decimal("0.05")
    daily_budget_usd: Decimal = Decimal("2.50")
    audit_path: str = "/app/logs/featherless-audit.jsonl"

    def __post_init__(self) -> None:
        if self.model_id not in FEATHERLESS_MODEL_REGISTRY:
            raise ValueError("FEATHERLESS_MODEL is not in the pinned model registry")
        profile = FEATHERLESS_MODEL_REGISTRY[self.model_id]
        if self.timeout_seconds <= 0:
            raise ValueError("Featherless timeout must be positive")
        if self.base_url != "https://api.featherless.ai/v1":
            raise ValueError("Featherless base URL must use the approved HTTPS endpoint")
        if self.max_completion_tokens <= 0 or self.max_completion_tokens > profile.max_completion_tokens:
            raise ValueError("invalid Featherless completion-token limit")
        if self.max_prompt_chars <= 0 or self.max_prompt_chars + self.max_completion_tokens > profile.context_length:
            raise ValueError("Featherless prompt budget exceeds model context")
        if self.max_requests_per_trace <= 0 or self.max_total_tokens_per_trace <= 0:
            raise ValueError("Featherless request budgets must be positive")
        if self.max_cost_per_trace_usd <= 0 or self.daily_budget_usd <= 0:
            raise ValueError("Featherless cost budgets must be positive")
        if not self.audit_path:
            raise ValueError("Featherless audit path must be configured")

    @classmethod
    def from_env(cls) -> "FeatherlessSettings":
        enabled = os.getenv("FEATHERLESS_ANALYSIS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            api_key=os.getenv("FEATHERLESS_API_KEY", "").strip(),
            base_url=os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1").rstrip("/"),
            model_id=os.getenv("FEATHERLESS_MODEL", DEFAULT_MODEL_ID).strip(),
            timeout_seconds=float(os.getenv("FEATHERLESS_TIMEOUT_SECONDS", "30")),
            max_completion_tokens=int(os.getenv("FEATHERLESS_MAX_COMPLETION_TOKENS", "700")),
            max_prompt_chars=int(os.getenv("FEATHERLESS_MAX_PROMPT_CHARS", "24000")),
            max_requests_per_trace=int(os.getenv("FEATHERLESS_MAX_REQUESTS_PER_TRACE", "3")),
            max_total_tokens_per_trace=int(os.getenv("FEATHERLESS_MAX_TOTAL_TOKENS_PER_TRACE", "75000")),
            max_cost_per_trace_usd=Decimal(os.getenv("FEATHERLESS_MAX_COST_PER_TRACE_USD", "0.05")),
            daily_budget_usd=Decimal(os.getenv("FEATHERLESS_DAILY_BUDGET_USD", "2.50")),
            audit_path=os.getenv("FEATHERLESS_AUDIT_PATH", "/app/logs/featherless-audit.jsonl").strip(),
        )


@dataclass(frozen=True, slots=True)
class FeatherlessAuditRecord:
    record_id: str
    trace_id: str
    agent_name: str
    model_id: str
    model_registry_version: str
    prompt_version: str
    evidence_fingerprint: str
    prompt_sha256: str
    response_sha256: str
    provider_request_id: str
    outcome: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: Decimal
    error_code: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.record_id or not self.trace_id or not self.agent_name or not self.model_id:
            raise ValueError("Featherless audit identity fields are required")
        for name in ("prompt_sha256", "evidence_fingerprint"):
            digest = getattr(self, name)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.response_sha256 and (
            len(self.response_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.response_sha256)
        ):
            raise ValueError("response_sha256 must be empty or a lowercase SHA-256 digest")
        if self.outcome not in {"analyze", "abstain", "error"}:
            raise ValueError("invalid Featherless audit outcome")
        if min(self.prompt_tokens, self.completion_tokens, self.total_tokens) < 0:
            raise ValueError("Featherless token usage cannot be negative")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("Featherless audit token totals are inconsistent")
        if self.estimated_cost_usd < 0:
            raise ValueError("Featherless audit cost cannot be negative")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timezone.utc.utcoffset(self.created_at):
            raise ValueError("Featherless audit timestamp must be UTC")


@dataclass(frozen=True, slots=True)
class FeatherlessResult:
    draft: object
    audit: FeatherlessAuditRecord
