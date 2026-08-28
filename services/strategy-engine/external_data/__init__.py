"""Source-attributed, non-executing research provider adapters."""

from .common import (
    MemoryEvidenceCache,
    ProviderDisabled,
    ProviderRateLimited,
    ProviderUnavailable,
)
from .finnhub import FinnhubAdapter, FinnhubSettings
from .fred import (
    FRED_REGISTRY_VERSION,
    FredAdapter,
    FredSeriesDefinition,
    FredSettings,
    MacroAssessment,
    MacroRegime,
    MacroRegimeEngine,
)
from .quality import DataQualityEngine, DataQualityPolicy, DataQualityReport, EvidenceDisagreement
from .yfinance_adapter import YFinanceAdapter, YFinanceSettings

__all__ = [
    "DataQualityEngine",
    "DataQualityPolicy",
    "DataQualityReport",
    "EvidenceDisagreement",
    "FRED_REGISTRY_VERSION",
    "FinnhubAdapter",
    "FinnhubSettings",
    "FredAdapter",
    "FredSeriesDefinition",
    "FredSettings",
    "MacroAssessment",
    "MacroRegime",
    "MacroRegimeEngine",
    "MemoryEvidenceCache",
    "ProviderDisabled",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "YFinanceAdapter",
    "YFinanceSettings",
]
