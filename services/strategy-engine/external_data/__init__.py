"""Source-attributed, non-executing research provider adapters."""

from .common import (
    MemoryEvidenceCache,
    ProviderDisabled,
    ProviderRateLimited,
    ProviderUnavailable,
)
from .finnhub import FinnhubAdapter, FinnhubSettings
from .quality import DataQualityEngine, DataQualityPolicy, DataQualityReport, EvidenceDisagreement
from .yfinance_adapter import YFinanceAdapter, YFinanceSettings

__all__ = [
    "DataQualityEngine",
    "DataQualityPolicy",
    "DataQualityReport",
    "EvidenceDisagreement",
    "FinnhubAdapter",
    "FinnhubSettings",
    "MemoryEvidenceCache",
    "ProviderDisabled",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "YFinanceAdapter",
    "YFinanceSettings",
]
