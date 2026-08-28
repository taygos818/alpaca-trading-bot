"""Thread-safe worst-case request and spend reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import threading

from .models import FeatherlessBudgetExceeded, FeatherlessModelProfile, FeatherlessSettings


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    prompt_tokens: int
    total_tokens: int
    maximum_cost_usd: Decimal


@dataclass(slots=True)
class _TraceSpend:
    requests: int = 0
    tokens: int = 0
    cost: Decimal = Decimal("0")


class FeatherlessBudgetGuard:
    """Reserves conservative upper bounds before any paid request is made."""

    def __init__(self, settings: FeatherlessSettings, profile: FeatherlessModelProfile) -> None:
        self.settings = settings
        self.profile = profile
        self._traces: dict[str, _TraceSpend] = {}
        self._daily: dict[str, Decimal] = {}
        self._lock = threading.Lock()

    def reserve(self, trace_id: str, prompt: str, now: datetime) -> BudgetReservation:
        if len(prompt) > self.settings.max_prompt_chars:
            raise FeatherlessBudgetExceeded("prompt exceeds configured character budget")
        # Prompts are canonical ASCII. One token per character is deliberately conservative.
        prompt_tokens = len(prompt)
        total_tokens = prompt_tokens + self.settings.max_completion_tokens
        maximum_cost = (
            Decimal(prompt_tokens) * self.profile.prompt_price_per_token_usd
            + Decimal(self.settings.max_completion_tokens) * self.profile.completion_price_per_token_usd
        )
        day = now.date().isoformat()
        with self._lock:
            spend = self._traces.setdefault(trace_id, _TraceSpend())
            if spend.requests + 1 > self.settings.max_requests_per_trace:
                raise FeatherlessBudgetExceeded("request count exceeds per-trace budget")
            if spend.tokens + total_tokens > self.settings.max_total_tokens_per_trace:
                raise FeatherlessBudgetExceeded("tokens exceed per-trace budget")
            if spend.cost + maximum_cost > self.settings.max_cost_per_trace_usd:
                raise FeatherlessBudgetExceeded("cost exceeds per-trace budget")
            if self._daily.get(day, Decimal("0")) + maximum_cost > self.settings.daily_budget_usd:
                raise FeatherlessBudgetExceeded("cost exceeds daily budget")
            spend.requests += 1
            spend.tokens += total_tokens
            spend.cost += maximum_cost
            self._daily[day] = self._daily.get(day, Decimal("0")) + maximum_cost
        return BudgetReservation(prompt_tokens, total_tokens, maximum_cost)
