# Data Provider Plan

## Source authority

| Provider | Intended role | May independently authorize execution? | Failure behavior |
|---|---|---:|---|
| Alpaca | Broker state, orders, fills, executable underlying and option quotes, options chain | No; mandatory input to deterministic gates | Fail closed for entries; continue reconciliation and protected exits where possible |
| Finnhub | Premarket discovery, company news, catalysts, earnings and event enrichment | No | Remove unavailable features or candidate according to declared dependency |
| yfinance | Secondary historical research and cross-checking | No | Degrade without interrupting broker reconciliation or exits |

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

## Milestone 3C — Data quality and fusion

Every evidence item records provider, instrument, event time, receipt time, delay/entitlement, session, raw-value hash, transformation version, freshness result, and correlation ID. Cross-source disagreement is visible to agents and can veto a proposal; values are never silently averaged.
