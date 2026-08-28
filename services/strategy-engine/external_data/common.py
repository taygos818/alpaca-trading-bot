"""Shared provider errors, cache, hashing, and evidence construction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import threading
import time
from typing import Any, Callable

import requests

from agent_contracts import EvidenceItem, canonical_json


class ProviderUnavailable(RuntimeError):
    pass


class ProviderDisabled(ProviderUnavailable):
    pass


class ProviderRateLimited(ProviderUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    evidence: tuple[EvidenceItem, ...]


class MemoryEvidenceCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[EvidenceItem, ...] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                return None
            return entry.evidence

    def put(self, key: str, evidence: tuple[EvidenceItem, ...], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("cache TTL must be positive")
        with self._lock:
            self._entries[key] = _CacheEntry(self._clock() + ttl_seconds, evidence)


def rebind_trace(evidence: tuple[EvidenceItem, ...], trace_id: str) -> tuple[EvidenceItem, ...]:
    """Attach cached immutable evidence to a new decision trace without changing source facts."""
    if all(item.trace_id == trace_id for item in evidence):
        return evidence
    rebound = []
    for item in evidence:
        rebound.append(
            replace(
                item,
                trace_id=trace_id,
                record_id=evidence_record_id(
                    item.provider,
                    trace_id,
                    item.instrument,
                    item.value_name,
                    item.value,
                ),
            )
        )
    return tuple(rebound)


def request_json(session, url: str, *, headers: dict[str, str], params: dict[str, Any], timeout: float) -> Any:
    try:
        response = session.get(url, headers=headers, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise ProviderUnavailable(f"provider request failed: {type(exc).__name__}") from exc
    if response.status_code == 429:
        raise ProviderRateLimited("provider rate limit reached")
    if response.status_code >= 400:
        raise ProviderUnavailable(f"provider returned HTTP {response.status_code}")
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise ProviderUnavailable("provider returned invalid JSON") from exc


def raw_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def evidence_record_id(provider: str, trace_id: str, instrument: str, value_name: str, payload: Any) -> str:
    digest = raw_sha256((provider, trace_id, instrument, value_name, payload))[:24]
    return f"evidence.{provider}.{digest}"


def utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ProviderUnavailable("provider timestamp is invalid")


def make_evidence(
    *,
    provider: str,
    trace_id: str,
    instrument: str,
    event_time: datetime,
    received_at: datetime,
    value_name: str,
    payload: Any,
    source_uri: str,
    entitlement: str,
    is_fresh: bool,
    authority: str,
    session: str,
    temporal_kind: str,
    transformation_version: str,
    numeric_value: Decimal | None = None,
    vintage: str = "",
) -> EvidenceItem:
    return EvidenceItem(
        record_id=evidence_record_id(provider, trace_id, instrument, value_name, payload),
        trace_id=trace_id,
        provider=provider,
        instrument=instrument.upper(),
        event_time=event_time,
        received_at=received_at,
        raw_sha256=raw_sha256(payload),
        value_name=value_name,
        value=canonical_json(payload),
        created_at=received_at,
        source_uri=source_uri,
        entitlement=entitlement,
        is_fresh=is_fresh,
        authority=authority,
        session=session,
        temporal_kind=temporal_kind,
        transformation_version=transformation_version,
        numeric_value=numeric_value,
        vintage=vintage,
    )
