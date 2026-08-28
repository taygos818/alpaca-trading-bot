"""Fail-closed Featherless research analysis boundary."""

from .budget import BudgetReservation, FeatherlessBudgetGuard
from .client import FeatherlessClient, JsonlAuditSink, MemoryAuditSink
from .integration import FeatherlessAnalysisRuntime, build_featherless_analysis_runtime
from .models import (
    DEFAULT_MODEL_ID,
    FEATHERLESS_MODEL_REGISTRY,
    MODEL_REGISTRY_VERSION,
    PROMPT_VERSION,
    FeatherlessAuditRecord,
    FeatherlessBudgetExceeded,
    FeatherlessDisabled,
    FeatherlessError,
    FeatherlessInvalidOutput,
    FeatherlessModelProfile,
    FeatherlessRateLimited,
    FeatherlessResult,
    FeatherlessSettings,
    FeatherlessUnavailable,
)
from .prompt import build_messages, parse_analysis

__all__ = [
    "BudgetReservation",
    "DEFAULT_MODEL_ID",
    "FEATHERLESS_MODEL_REGISTRY",
    "MODEL_REGISTRY_VERSION",
    "PROMPT_VERSION",
    "FeatherlessAuditRecord",
    "FeatherlessAnalysisRuntime",
    "FeatherlessBudgetExceeded",
    "FeatherlessBudgetGuard",
    "FeatherlessClient",
    "FeatherlessDisabled",
    "FeatherlessError",
    "FeatherlessInvalidOutput",
    "FeatherlessModelProfile",
    "FeatherlessRateLimited",
    "FeatherlessResult",
    "FeatherlessSettings",
    "FeatherlessUnavailable",
    "JsonlAuditSink",
    "MemoryAuditSink",
    "build_messages",
    "build_featherless_analysis_runtime",
    "parse_analysis",
]
