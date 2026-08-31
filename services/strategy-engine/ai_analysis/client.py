"""Narrow Featherless chat-completion adapter with fail-closed auditing."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import threading
from typing import Callable, Protocol

import requests

from agent_contracts import EvidenceBundle, EvidenceItem, canonical_json

from .budget import FeatherlessBudgetGuard
from .models import (
    FEATHERLESS_MODEL_REGISTRY,
    MODEL_REGISTRY_VERSION,
    PROMPT_VERSION,
    FeatherlessAuditRecord,
    FeatherlessBudgetExceeded,
    FeatherlessDisabled,
    FeatherlessInvalidOutput,
    FeatherlessRateLimited,
    FeatherlessResult,
    FeatherlessSettings,
    FeatherlessUnavailable,
)
from .prompt import build_messages, parse_analysis


class AuditSink(Protocol):
    def append(self, record: FeatherlessAuditRecord) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[FeatherlessAuditRecord] = []
        self._lock = threading.Lock()

    def append(self, record: FeatherlessAuditRecord) -> None:
        with self._lock:
            self.records.append(record)


class JsonlAuditSink:
    """Writes hashes and usage only; prompts, responses, and credentials are excluded."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: FeatherlessAuditRecord) -> None:
        payload = canonical_json(record)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")


class FeatherlessClient:
    def __init__(
        self,
        settings: FeatherlessSettings,
        *,
        session: requests.Session | None = None,
        budget: FeatherlessBudgetGuard | None = None,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.profile = FEATHERLESS_MODEL_REGISTRY[settings.model_id]
        # The requests module opens an independent connection per concurrent agent.
        # Injected sessions are reserved for deterministic tests or a caller-owned client.
        self.session = session or requests
        self.budget = budget or FeatherlessBudgetGuard(settings, self.profile)
        self.audit_sink = audit_sink or JsonlAuditSink(settings.audit_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def analyze(
        self,
        agent_name: str,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
    ) -> FeatherlessResult:
        attempts = self.settings.invalid_output_retries + 1
        for attempt in range(attempts):
            try:
                return self._analyze_once(agent_name, bundle, evidence)
            except FeatherlessInvalidOutput:
                if attempt + 1 >= attempts:
                    raise
        raise FeatherlessInvalidOutput("Featherless structured-output retry exhausted")

    def _analyze_once(
        self,
        agent_name: str,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
    ) -> FeatherlessResult:
        self._require_enabled()
        messages = build_messages(agent_name, bundle, evidence)
        prompt = canonical_json(messages)
        prompt_hash = _sha256(prompt)
        now = self.clock()
        reservation = None
        response_hash = ""
        request_id = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cost = Decimal("0")
        outcome = "error"
        error_code = "unknown"
        draft = None
        try:
            reservation = self.budget.reserve(bundle.trace_id, prompt, now)
            response = self.session.post(
                f"{self.settings.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/taygos818/alpaca-trading-bot",
                    "X-Title": "Alpaca Trading Bot",
                },
                json={
                    "model": self.settings.model_id,
                    "messages": list(messages),
                    "temperature": 0,
                    "seed": 42,
                    "max_tokens": self.settings.max_completion_tokens,
                },
                timeout=self.settings.timeout_seconds,
            )
            request_id = str(response.headers.get("x-request-id", ""))
            if response.status_code == 429:
                raise FeatherlessRateLimited("Featherless rate limit reached")
            if response.status_code >= 400:
                raise FeatherlessUnavailable(f"Featherless returned HTTP {response.status_code}")
            try:
                payload = response.json()
                choice = payload["choices"][0]
                content = choice["message"]["content"]
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise FeatherlessInvalidOutput("Featherless response envelope is invalid") from exc
            if payload.get("model") != self.settings.model_id:
                raise FeatherlessInvalidOutput("Featherless response model does not match the pinned request")
            if choice.get("finish_reason") != "stop":
                raise FeatherlessInvalidOutput("Featherless response did not finish cleanly")
            response_hash = _sha256(canonical_json(payload))
            usage = _usage(payload.get("usage"))
            if usage["completion_tokens"] > self.settings.max_completion_tokens:
                raise FeatherlessInvalidOutput("Featherless completion exceeded its token limit")
            if usage["total_tokens"] > self.profile.context_length:
                raise FeatherlessInvalidOutput("Featherless response exceeded model context")
            cost = (
                Decimal(usage["prompt_tokens"]) * self.profile.prompt_price_per_token_usd
                + Decimal(usage["completion_tokens"]) * self.profile.completion_price_per_token_usd
            )
            draft = parse_analysis(content, bundle.evidence_ids)
            outcome = draft.disposition.value
            error_code = ""
        except requests.Timeout as exc:
            error_code = "timeout"
            raise FeatherlessUnavailable("Featherless request timed out") from exc
        except requests.RequestException as exc:
            error_code = "network"
            raise FeatherlessUnavailable(f"Featherless request failed: {type(exc).__name__}") from exc
        except FeatherlessBudgetExceeded:
            error_code = "budget"
            raise
        except FeatherlessRateLimited:
            error_code = "rate_limit"
            raise
        except FeatherlessInvalidOutput:
            error_code = "invalid_output"
            raise
        except FeatherlessUnavailable:
            error_code = "unavailable"
            raise
        finally:
            audit = self._audit(
                bundle=bundle,
                agent_name=agent_name,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                request_id=request_id,
                outcome=outcome,
                usage=usage,
                cost=cost if cost else (reservation.maximum_cost_usd if reservation is not None else Decimal("0")),
                error_code=error_code,
                created_at=now,
            )
            self.audit_sink.append(audit)
        return FeatherlessResult(draft=draft, audit=audit)

    def evaluator(self, agent_name: str):
        def evaluate(bundle: EvidenceBundle, evidence: tuple[EvidenceItem, ...]):
            return self.analyze(agent_name, bundle, evidence).draft

        return evaluate

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise FeatherlessDisabled("Featherless analysis is disabled")
        if not self.settings.api_key:
            raise FeatherlessUnavailable("FEATHERLESS_API_KEY is missing")

    def _audit(self, **values) -> FeatherlessAuditRecord:
        identity = canonical_json(
            {
                "trace_id": values["bundle"].trace_id,
                "agent_name": values["agent_name"],
                "model_id": self.settings.model_id,
                "prompt_sha256": values["prompt_hash"],
                "created_at": values["created_at"],
            }
        )
        return FeatherlessAuditRecord(
            record_id=f"ai-audit.{_sha256(identity)[:24]}",
            trace_id=values["bundle"].trace_id,
            agent_name=values["agent_name"],
            model_id=self.settings.model_id,
            model_registry_version=MODEL_REGISTRY_VERSION,
            prompt_version=PROMPT_VERSION,
            evidence_fingerprint=values["bundle"].evidence_fingerprint,
            prompt_sha256=values["prompt_hash"],
            response_sha256=values["response_hash"],
            provider_request_id=values["request_id"],
            outcome=values["outcome"],
            prompt_tokens=values["usage"]["prompt_tokens"],
            completion_tokens=values["usage"]["completion_tokens"],
            total_tokens=values["usage"]["total_tokens"],
            estimated_cost_usd=values["cost"],
            error_code=values["error_code"],
            created_at=values["created_at"],
        )


def _usage(payload) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise FeatherlessInvalidOutput("Featherless usage metadata is missing")
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FeatherlessInvalidOutput("Featherless usage metadata is invalid")
        result[key] = value
    if result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]:
        raise FeatherlessInvalidOutput("Featherless token totals are inconsistent")
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
