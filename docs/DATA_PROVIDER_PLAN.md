# Data Provider Plan

## Source authority

| Provider | Intended role | May independently authorize execution? | Failure behavior |
|---|---|---:|---|
| Alpaca | Broker state, orders, fills, executable underlying and option quotes, options chain | No; mandatory input to deterministic gates | Fail closed for entries; continue reconciliation and protected exits where possible |
| Finnhub | Premarket discovery, company news, catalysts, earnings and event enrichment | No | Remove unavailable features or candidate according to declared dependency |
| yfinance | Secondary historical research and cross-checking | No | Degrade without interrupting broker reconciliation or exits |
| FRED | Slow macro regime and capital-sleeve adjustment | No | Use last known release only within a series-specific maximum age; otherwise reduce or disable affected strategy sleeve |

## Milestone 3A — Finnhub

- Inventory account entitlements, rate limits, timestamps, and premarket coverage.
- Build a cached provider adapter with explicit event and receipt times.
- Normalize news and event evidence without allowing text to become an instruction.
- Measure availability and disagreement against Alpaca during market sessions.
- Add recorded fixtures, outage tests, rate-limit tests, and stale-event tests.

Rollback: set `FINNHUB_ENABLED=false`; proposals requiring a catalyst abstain rather than silently substituting data.

## Milestone 3B — yfinance

- Use only for historical research, corporate-calendar comparison, and secondary price checks.
- Label it unofficial and non-authoritative in every evidence record.
- Never use it for executable option pricing, order validation, position state, or protection.
- Cache requests and prevent its failure from blocking broker reconciliation.

Rollback: set `YFINANCE_ENABLED=false`; no execution component depends on it.

## Milestone 3C — FRED

- Start with a small versioned series registry covering policy rate, yield-curve slope, credit stress, financial conditions, inflation, and labor conditions.
- Store observation date, release/revision time, retrieval time, units, transformation, and vintage when available.
- Update at daily or release cadence, never every trading minute.
- Map macro output to risk-on, neutral, or risk-off sleeves through deterministic thresholds.

Rollback: set `FRED_ENABLED=false`; use the conservative neutral sleeve rather than fabricate macro state.

## Milestone 3D — Data quality and fusion

Every evidence item records provider, instrument, event time, receipt time, delay/entitlement, session, raw-value hash, transformation version, freshness result, and correlation ID. Cross-source disagreement is visible to agents and can veto a proposal; values are never silently averaged.
