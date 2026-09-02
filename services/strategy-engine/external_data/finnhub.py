"""Finnhub catalyst adapter. News text is evidence, never an instruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import os
import requests

from agent_contracts import EvidenceItem

from .common import (
    MemoryEvidenceCache,
    ProviderDisabled,
    ProviderUnavailable,
    make_evidence,
    rebind_trace,
    request_json,
    utc_datetime,
)


@dataclass(frozen=True, slots=True)
class FinnhubSettings:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://finnhub.io/api/v1"
    entitlement: str = "unknown"
    timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 300
    max_news_age_hours: int = 72

    @classmethod
    def from_env(cls) -> "FinnhubSettings":
        enabled = os.getenv("FINNHUB_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
            base_url=os.getenv("FINNHUB_BASE_URL", "https://finnhub.io/api/v1").rstrip("/"),
            entitlement=os.getenv("FINNHUB_ENTITLEMENT", "unknown").strip() or "unknown",
            timeout_seconds=float(os.getenv("FINNHUB_TIMEOUT_SECONDS", "10")),
            cache_ttl_seconds=int(os.getenv("FINNHUB_CACHE_TTL_SECONDS", "300")),
            max_news_age_hours=int(os.getenv("FINNHUB_MAX_NEWS_AGE_HOURS", "72")),
        )


class FinnhubAdapter:
    provider_name = "finnhub"

    def __init__(
        self,
        settings: FinnhubSettings,
        *,
        session: requests.Session | None = None,
        cache: MemoryEvidenceCache | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.cache = cache or MemoryEvidenceCache()

    def company_news(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        trace_id: str,
        received_at: datetime,
    ) -> tuple[EvidenceItem, ...]:
        self._require_enabled()
        cache_key = f"finnhub:news:{symbol.upper()}:{start}:{end}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return rebind_trace(cached, trace_id)
        payload = request_json(
            self.session,
            f"{self.settings.base_url}/company-news",
            headers={"X-Finnhub-Token": self.settings.api_key},
            params={"symbol": symbol.upper(), "from": start.isoformat(), "to": end.isoformat()},
            timeout=self.settings.timeout_seconds,
        )
        if not isinstance(payload, list):
            raise ProviderUnavailable("Finnhub company-news response must be a list")
        evidence = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("datetime") or not item.get("headline"):
                continue
            event_time = utc_datetime(item["datetime"])
            if event_time > received_at:
                continue
            normalized = {
                "category": str(item.get("category") or ""),
                "headline": str(item["headline"]),
                "id": str(item.get("id") or ""),
                "related": str(item.get("related") or symbol.upper()),
                "source": str(item.get("source") or ""),
                "summary": str(item.get("summary") or ""),
                "url": str(item.get("url") or ""),
            }
            evidence.append(
                make_evidence(
                    provider=self.provider_name,
                    trace_id=trace_id,
                    instrument=symbol,
                    event_time=event_time,
                    received_at=received_at,
                    value_name="company_news_event",
                    payload=normalized,
                    source_uri=normalized["url"] or f"{self.settings.base_url}/company-news",
                    entitlement=self.settings.entitlement,
                    is_fresh=event_time >= received_at - timedelta(hours=self.settings.max_news_age_hours),
                    authority="licensed_research",
                    session="event",
                    temporal_kind="observed",
                    transformation_version="finnhub-company-news-v1",
                )
            )
        result = tuple(sorted(evidence, key=lambda item: (item.event_time, item.record_id)))
        self.cache.put(cache_key, result, self.settings.cache_ttl_seconds)
        return result

    def earnings_calendar(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        trace_id: str,
        received_at: datetime,
    ) -> tuple[EvidenceItem, ...]:
        self._require_enabled()
        cache_key = f"finnhub:earnings:{symbol.upper()}:{start}:{end}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return rebind_trace(cached, trace_id)
        payload = request_json(
            self.session,
            f"{self.settings.base_url}/calendar/earnings",
            headers={"X-Finnhub-Token": self.settings.api_key},
            params={"symbol": symbol.upper(), "from": start.isoformat(), "to": end.isoformat()},
            timeout=self.settings.timeout_seconds,
        )
        rows = payload.get("earningsCalendar", []) if isinstance(payload, dict) else []
        evidence = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("date"):
                continue
            event_date = date.fromisoformat(str(row["date"]))
            event_time = datetime.combine(event_date, time(12), tzinfo=timezone.utc)
            normalized = {
                "date": event_date.isoformat(),
                "epsEstimate": row.get("epsEstimate"),
                "hour": str(row.get("hour") or ""),
                "quarter": row.get("quarter"),
                "revenueEstimate": row.get("revenueEstimate"),
                "symbol": str(row.get("symbol") or symbol.upper()),
                "year": row.get("year"),
            }
            evidence.append(
                make_evidence(
                    provider=self.provider_name,
                    trace_id=trace_id,
                    instrument=symbol,
                    event_time=event_time,
                    received_at=received_at,
                    value_name="earnings_calendar_event",
                    payload=normalized,
                    source_uri=f"{self.settings.base_url}/calendar/earnings",
                    entitlement=self.settings.entitlement,
                    is_fresh=start <= event_date <= end,
                    authority="licensed_research",
                    session="event",
                    temporal_kind="scheduled" if event_time > received_at else "historical",
                    transformation_version="finnhub-earnings-calendar-v1",
                )
            )
        result = tuple(sorted(evidence, key=lambda item: (item.event_time, item.record_id)))
        self.cache.put(cache_key, result, self.settings.cache_ttl_seconds)
        return result

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ProviderDisabled("Finnhub is disabled")
        if not self.settings.api_key:
            raise ProviderUnavailable("Finnhub API key is missing")
