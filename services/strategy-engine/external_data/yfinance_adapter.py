"""Unofficial yfinance adapter for secondary research and cross-checks only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import math
import os
from typing import Any, Callable

from agent_contracts import EvidenceItem

from .common import MemoryEvidenceCache, ProviderDisabled, ProviderUnavailable, make_evidence, rebind_trace, utc_datetime


TickerFactory = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class YFinanceSettings:
    enabled: bool = False
    cache_ttl_seconds: int = 900
    timeout_seconds: float = 10.0
    max_history_age_days: int = 400
    max_calendar_horizon_days: int = 120

    @classmethod
    def from_env(cls) -> "YFinanceSettings":
        enabled = os.getenv("YFINANCE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            cache_ttl_seconds=int(os.getenv("YFINANCE_CACHE_TTL_SECONDS", "900")),
            timeout_seconds=float(os.getenv("YFINANCE_TIMEOUT_SECONDS", "10")),
            max_history_age_days=int(os.getenv("YFINANCE_MAX_HISTORY_AGE_DAYS", "400")),
            max_calendar_horizon_days=int(os.getenv("YFINANCE_MAX_CALENDAR_HORIZON_DAYS", "120")),
        )


class YFinanceAdapter:
    provider_name = "yfinance"

    def __init__(
        self,
        settings: YFinanceSettings,
        *,
        ticker_factory: TickerFactory | None = None,
        cache: MemoryEvidenceCache | None = None,
    ) -> None:
        self.settings = settings
        self._ticker_factory = ticker_factory
        self.cache = cache or MemoryEvidenceCache()

    def history(
        self,
        symbol: str,
        *,
        trace_id: str,
        received_at: datetime,
        period: str = "1mo",
        interval: str = "1d",
    ) -> tuple[EvidenceItem, ...]:
        self._require_enabled()
        cache_key = f"yfinance:history:{symbol.upper()}:{period}:{interval}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return rebind_trace(cached, trace_id)
        try:
            frame = self._ticker(symbol).history(
                period=period,
                interval=interval,
                auto_adjust=False,
                actions=False,
                repair=True,
                timeout=self.settings.timeout_seconds,
            )
            rows = list(frame.iterrows())
        except Exception as exc:
            raise ProviderUnavailable(f"yfinance history failed: {type(exc).__name__}") from exc
        evidence = []
        for index, row in rows:
            event_time = utc_datetime(index.to_pydatetime() if hasattr(index, "to_pydatetime") else index)
            normalized = {
                "open": _finite_number(_row_value(row, "Open")),
                "high": _finite_number(_row_value(row, "High")),
                "low": _finite_number(_row_value(row, "Low")),
                "close": _finite_number(_row_value(row, "Close")),
                "volume": _finite_number(_row_value(row, "Volume")),
                "interval": interval,
            }
            if normalized["close"] is None:
                continue
            evidence.append(
                make_evidence(
                    provider=self.provider_name,
                    trace_id=trace_id,
                    instrument=symbol,
                    event_time=event_time,
                    received_at=received_at,
                    value_name="historical_close",
                    payload=normalized,
                    source_uri=f"https://finance.yahoo.com/quote/{symbol.upper()}/history",
                    entitlement="unofficial-personal-research",
                    is_fresh=event_time >= received_at - timedelta(days=self.settings.max_history_age_days),
                    authority="unofficial_research",
                    session="daily" if interval.endswith("d") else "unknown",
                    temporal_kind="historical",
                    transformation_version="yfinance-history-v1",
                    numeric_value=Decimal(str(normalized["close"])),
                )
            )
        result = tuple(sorted(evidence, key=lambda item: (item.event_time, item.record_id)))
        self.cache.put(cache_key, result, self.settings.cache_ttl_seconds)
        return result

    def calendar(
        self,
        symbol: str,
        *,
        trace_id: str,
        received_at: datetime,
    ) -> tuple[EvidenceItem, ...]:
        self._require_enabled()
        cache_key = f"yfinance:calendar:{symbol.upper()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return rebind_trace(cached, trace_id)
        try:
            calendar = self._ticker(symbol).calendar
        except Exception as exc:
            raise ProviderUnavailable(f"yfinance calendar failed: {type(exc).__name__}") from exc
        if not isinstance(calendar, dict):
            raise ProviderUnavailable("yfinance calendar response must be a mapping")
        raw_dates = calendar.get("Earnings Date")
        if raw_dates is None:
            raw_dates = calendar.get("EarningsDate")
        if raw_dates is None:
            raw_dates = ()
        if not isinstance(raw_dates, (list, tuple)):
            raw_dates = (raw_dates,)
        evidence = []
        for raw_date in raw_dates:
            if raw_date is None:
                continue
            event_time = utc_datetime(raw_date.to_pydatetime() if hasattr(raw_date, "to_pydatetime") else raw_date)
            normalized = {
                "event": "earnings",
                "event_time": event_time.isoformat(),
                "source": "yfinance-calendar",
            }
            horizon = event_time - received_at
            evidence.append(
                make_evidence(
                    provider=self.provider_name,
                    trace_id=trace_id,
                    instrument=symbol,
                    event_time=event_time,
                    received_at=received_at,
                    value_name="earnings_calendar_event",
                    payload=normalized,
                    source_uri=f"https://finance.yahoo.com/quote/{symbol.upper()}",
                    entitlement="unofficial-personal-research",
                    is_fresh=timedelta(days=-1) <= horizon <= timedelta(days=self.settings.max_calendar_horizon_days),
                    authority="unofficial_research",
                    session="event",
                    temporal_kind="scheduled" if event_time > received_at else "historical",
                    transformation_version="yfinance-calendar-v1",
                )
            )
        result = tuple(sorted(evidence, key=lambda item: (item.event_time, item.record_id)))
        self.cache.put(cache_key, result, self.settings.cache_ttl_seconds)
        return result

    def _ticker(self, symbol: str):
        if self._ticker_factory is not None:
            return self._ticker_factory(symbol.upper())
        try:
            import yfinance
        except ImportError as exc:
            raise ProviderUnavailable("yfinance dependency is unavailable") from exc
        return yfinance.Ticker(symbol.upper())

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ProviderDisabled("yfinance is disabled")


def _row_value(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


def _finite_number(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number.is_integer():
        return int(number)
    return number
