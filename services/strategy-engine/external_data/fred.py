"""Vintage-aware FRED macro evidence and deterministic regime mapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import os

import requests

from agent_contracts import EvidenceItem

from .common import MemoryEvidenceCache, ProviderDisabled, ProviderUnavailable, make_evidence, rebind_trace, request_json


FRED_REGISTRY_VERSION = "fred-registry-2026-08-28-v1"


@dataclass(frozen=True, slots=True)
class FredSeriesDefinition:
    key: str
    series_id: str
    units: str
    transformation: str
    max_age_days: int
    risk_off_above: Decimal | None = None
    risk_off_below: Decimal | None = None
    risk_on_above: Decimal | None = None
    risk_on_below: Decimal | None = None


FRED_SERIES_REGISTRY = (
    FredSeriesDefinition("policy_rate", "DFF", "percent", "lin", 10),
    FredSeriesDefinition("yield_curve", "T10Y2Y", "percent", "lin", 10, risk_off_below=Decimal("0"), risk_on_above=Decimal("0.50")),
    FredSeriesDefinition("credit_stress", "BAMLH0A0HYM2", "percent", "lin", 10, risk_off_above=Decimal("5.00"), risk_on_below=Decimal("3.50")),
    FredSeriesDefinition("financial_conditions", "NFCI", "index", "lin", 14, risk_off_above=Decimal("0.50"), risk_on_below=Decimal("0")),
    FredSeriesDefinition("inflation", "CPIAUCSL", "percent_change_year_ago", "pc1", 75, risk_off_above=Decimal("4.00"), risk_on_below=Decimal("2.50")),
    FredSeriesDefinition("labor", "UNRATE", "percent", "lin", 45, risk_off_above=Decimal("6.00"), risk_on_below=Decimal("4.50")),
)


class MacroRegime(str, Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"


@dataclass(frozen=True, slots=True)
class MacroAssessment:
    regime: MacroRegime
    sleeve_multiplier: Decimal
    risk_on_signals: tuple[str, ...]
    risk_off_signals: tuple[str, ...]
    missing_series: tuple[str, ...]
    registry_version: str = FRED_REGISTRY_VERSION


@dataclass(frozen=True, slots=True)
class FredSettings:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.stlouisfed.org/fred"
    timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 86400

    @classmethod
    def from_env(cls) -> "FredSettings":
        enabled = os.getenv("FRED_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            api_key=os.getenv("FRED_API_KEY", "").strip(),
            base_url=os.getenv("FRED_API_URL", "https://api.stlouisfed.org/fred").rstrip("/"),
            timeout_seconds=float(os.getenv("FRED_TIMEOUT_SECONDS", "10")),
            cache_ttl_seconds=int(os.getenv("FRED_CACHE_TTL_SECONDS", "86400")),
        )


class FredAdapter:
    provider_name = "fred"

    def __init__(
        self,
        settings: FredSettings,
        *,
        registry: tuple[FredSeriesDefinition, ...] = FRED_SERIES_REGISTRY,
        session: requests.Session | None = None,
        cache: MemoryEvidenceCache | None = None,
    ) -> None:
        self.settings = settings
        self.registry = {item.key: item for item in registry}
        self.session = session or requests.Session()
        self.cache = cache or MemoryEvidenceCache()

    def fetch(self, key: str, *, trace_id: str, received_at: datetime) -> tuple[EvidenceItem, ...]:
        self._require_enabled()
        try:
            definition = self.registry[key]
        except KeyError as exc:
            raise ProviderUnavailable(f"FRED series key is not registered: {key}") from exc
        cache_key = f"fred:{FRED_REGISTRY_VERSION}:{key}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return rebind_trace(cached, trace_id)
        common_params = {"api_key": self.settings.api_key, "file_type": "json", "series_id": definition.series_id}
        observations_payload = request_json(
            self.session,
            f"{self.settings.base_url}/series/observations",
            headers={},
            params={**common_params, "sort_order": "desc", "limit": 10, "units": definition.transformation},
            timeout=self.settings.timeout_seconds,
        )
        vintage_payload = request_json(
            self.session,
            f"{self.settings.base_url}/series/vintagedates",
            headers={},
            params={**common_params, "sort_order": "desc", "limit": 1},
            timeout=self.settings.timeout_seconds,
        )
        observations = observations_payload.get("observations", []) if isinstance(observations_payload, dict) else []
        numeric = next((row for row in observations if str(row.get("value", "")).strip() not in {"", "."}), None)
        if numeric is None:
            raise ProviderUnavailable(f"FRED returned no numeric observation for {key}")
        try:
            value = Decimal(str(numeric["value"]))
            observation_date = date.fromisoformat(str(numeric["date"]))
        except Exception as exc:
            raise ProviderUnavailable(f"FRED returned invalid observation for {key}") from exc
        vintage_dates = vintage_payload.get("vintage_dates", []) if isinstance(vintage_payload, dict) else []
        vintage = str(vintage_dates[0]) if vintage_dates else str(numeric.get("realtime_start") or "")
        event_time = datetime.combine(observation_date, time(0), tzinfo=timezone.utc)
        payload = {
            "key": key,
            "series_id": definition.series_id,
            "value": format(value, "f"),
            "observation_date": observation_date.isoformat(),
            "realtime_start": str(numeric.get("realtime_start") or ""),
            "realtime_end": str(numeric.get("realtime_end") or ""),
            "units": definition.units,
            "transformation": definition.transformation,
            "vintage": vintage,
        }
        item = make_evidence(
            provider=self.provider_name,
            trace_id=trace_id,
            instrument=f"FRED:{definition.series_id}",
            event_time=event_time,
            received_at=received_at,
            value_name=f"fred.{key}",
            payload=payload,
            source_uri=f"{self.settings.base_url}/series/observations?series_id={definition.series_id}",
            entitlement="fred-api",
            is_fresh=event_time >= received_at - timedelta(days=definition.max_age_days),
            authority="macro_research",
            session="daily",
            temporal_kind="release",
            transformation_version=FRED_REGISTRY_VERSION,
            numeric_value=value,
            vintage=vintage,
        )
        result = (item,)
        self.cache.put(cache_key, result, self.settings.cache_ttl_seconds)
        return result

    def fetch_all(self, *, trace_id: str, received_at: datetime) -> tuple[EvidenceItem, ...]:
        evidence = []
        for key in sorted(self.registry):
            evidence.extend(self.fetch(key, trace_id=trace_id, received_at=received_at))
        return tuple(evidence)

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ProviderDisabled("FRED is disabled")
        if not self.settings.api_key:
            raise ProviderUnavailable("FRED API key is missing")


class MacroRegimeEngine:
    def __init__(self, registry: tuple[FredSeriesDefinition, ...] = FRED_SERIES_REGISTRY) -> None:
        self.registry = {item.key: item for item in registry}

    def evaluate(self, evidence: tuple[EvidenceItem, ...]) -> MacroAssessment:
        values = {
            item.value_name.removeprefix("fred."): item.numeric_value
            for item in evidence
            if item.provider == "fred" and item.value_name.startswith("fred.") and item.is_fresh
        }
        risk_on = []
        risk_off = []
        missing = []
        for key, definition in sorted(self.registry.items()):
            value = values.get(key)
            if value is None:
                missing.append(key)
                continue
            if definition.risk_off_above is not None and value > definition.risk_off_above:
                risk_off.append(key)
            if definition.risk_off_below is not None and value < definition.risk_off_below:
                risk_off.append(key)
            if definition.risk_on_above is not None and value > definition.risk_on_above:
                risk_on.append(key)
            if definition.risk_on_below is not None and value < definition.risk_on_below:
                risk_on.append(key)
        if len(risk_off) >= 2:
            regime, multiplier = MacroRegime.RISK_OFF, Decimal("0.25")
        elif len(risk_on) >= 3 and not risk_off and len(missing) <= 1:
            regime, multiplier = MacroRegime.RISK_ON, Decimal("1.00")
        else:
            regime, multiplier = MacroRegime.NEUTRAL, Decimal("0.50")
        return MacroAssessment(
            regime=regime,
            sleeve_multiplier=multiplier,
            risk_on_signals=tuple(risk_on),
            risk_off_signals=tuple(risk_off),
            missing_series=tuple(missing),
        )
