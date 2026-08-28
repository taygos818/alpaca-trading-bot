"""Append-only, credential-free decision trace journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any

from agent_contracts import DecisionTrace, canonical_json, contract_fingerprint


FORBIDDEN_KEYS = {
    "alpaca_api_key",
    "alpaca_secret_key",
    "featherless_api_key",
    "finnhub_api_key",
    "fred_api_key",
}


@dataclass(frozen=True, slots=True)
class JournalRecord:
    trace_id: str
    phase: str
    outcome: str
    fingerprint: str
    recorded_at: datetime
    trace: dict[str, Any]
    metadata: dict[str, Any]


class DecisionTraceJournal:
    """Persists immutable trace revisions; latest revision is the dashboard view."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(
        self,
        trace: DecisionTrace,
        *,
        phase: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> JournalRecord:
        if not phase or not outcome:
            raise ValueError("journal phase and outcome are required")
        safe_metadata = metadata or {}
        _reject_sensitive_keys(safe_metadata)
        timestamp = recorded_at or datetime.now(timezone.utc)
        payload = {
            "schema_version": "1.0",
            "trace_id": trace.bundle.trace_id,
            "phase": phase,
            "outcome": outcome,
            "fingerprint": trace.replay_fingerprint,
            "recorded_at": timestamp,
            "trace": json.loads(canonical_json(trace)),
            "metadata": safe_metadata,
        }
        encoded = canonical_json(payload)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
        return _decode_record(json.loads(encoded))

    def load_latest(self, limit: int = 100) -> tuple[JournalRecord, ...]:
        if limit <= 0:
            raise ValueError("journal limit must be positive")
        if not self.path.exists():
            return ()
        latest: dict[str, JournalRecord] = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record = _decode_record(json.loads(line))
                    latest[record.trace_id] = record
        return tuple(sorted(latest.values(), key=lambda item: item.recorded_at, reverse=True)[:limit])


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                raise ValueError("sensitive metadata key is forbidden")
            _reject_sensitive_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive_keys(nested)


def _decode_record(payload: dict[str, Any]) -> JournalRecord:
    timestamp = datetime.fromisoformat(str(payload["recorded_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    return JournalRecord(
        trace_id=payload["trace_id"],
        phase=payload["phase"],
        outcome=payload["outcome"],
        fingerprint=payload["fingerprint"],
        recorded_at=timestamp,
        trace=payload["trace"],
        metadata=payload.get("metadata", {}),
    )
