# External Data Provenance

Milestone 3 adds three independently gated research providers. Alpaca remains the sole broker authority for account state, positions, orders, fills, tradability, executable stock and option quotes, and final pre-submit validation.

## Provider boundaries

| Provider | Authority label | Permitted use | Prohibited use | Disable flag |
|---|---|---|---|---|
| Finnhub | `licensed_research` | News, catalysts, earnings, and event enrichment within the configured account entitlement | Broker state, executable pricing, or direct authorization | `FINNHUB_ENABLED=false` |
| yfinance | `unofficial_research` | Historical comparison and corporate-calendar cross-checks | Orders, positions, protection, executable option pricing, or sole-source authorization | `YFINANCE_ENABLED=false` |
| FRED | `macro_research` | Slow macro regime and deterministic sleeve adjustment | Intraday timing, executable pricing, or broker state | `FRED_ENABLED=false` |

All three flags default off. A provider outage or rate limit raises an explicit typed failure; it never silently substitutes mock data or reaches the execution gateway. Cached evidence preserves its original event time, receipt time, value hash, and transformation, while receiving a new record and trace identifier for the current decision cycle.

## Evidence and disagreement rules

Every normalized item records provider, authority, entitlement, instrument, event time, receipt time, session, temporal type, raw-value SHA-256, transformation version, freshness result, source URI, vintage when applicable, and a decision trace ID.

The central quality engine rejects stale evidence and evidence whose receipt is older than policy. Numeric observations for the same instrument, value, and event date are compared across providers. A relative difference above policy is retained as a visible disagreement and vetoes the bundle; the values are never averaged. Broker truth is still rechecked independently immediately before any future order submission.

## FRED registry and regime

Registry `fred-registry-2026-08-28-v1` covers DFF, T10Y2Y, BAMLH0A0HYM2, NFCI, CPIAUCSL, and UNRATE. Each entry declares its units, API transformation, maximum age, and transparent risk-on or risk-off threshold. Missing or inconclusive inputs produce a conservative neutral `0.50` sleeve multiplier; two risk-off signals reduce it to `0.25`; strong, sufficiently complete risk-on evidence permits `1.00`.

FRED observations are requested at daily/release cadence and include observation date, real-time revision fields, retrieval time, units, transformation, and the newest available vintage date. They are not polled every trading minute.

## Rollback

Disable the affected provider independently. Catalyst-dependent proposals abstain if Finnhub is unavailable. yfinance degradation does not interrupt reconciliation or exits. Missing FRED data returns the neutral macro sleeve instead of inventing state. Disabling all three restores Alpaca-only evidence without changing the broker or execution boundary.
