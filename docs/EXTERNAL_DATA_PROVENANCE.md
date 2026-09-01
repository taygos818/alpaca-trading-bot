# External Data Provenance

Milestone 3 adds two independently gated research providers. Alpaca remains the sole broker authority for account state, positions, orders, fills, tradability, executable stock and option quotes, and final pre-submit validation.

## Provider boundaries

| Provider | Authority label | Permitted use | Prohibited use | Disable flag |
|---|---|---|---|---|
| Finnhub | `licensed_research` | News, catalysts, earnings, and event enrichment within the configured account entitlement | Broker state, executable pricing, or direct authorization | `FINNHUB_ENABLED=false` |
| yfinance | `unofficial_research` | Historical comparison and corporate-calendar cross-checks | Orders, positions, protection, executable option pricing, or sole-source authorization | `YFINANCE_ENABLED=false` |

Both flags default off. A provider outage or rate limit raises an explicit typed failure; it never silently substitutes mock data or reaches the execution gateway. Cached evidence preserves its original event time, receipt time, value hash, and transformation, while receiving a new record and trace identifier for the current decision cycle.

## Evidence and disagreement rules

Every normalized item records provider, authority, entitlement, instrument, event time, receipt time, session, temporal type, raw-value SHA-256, transformation version, freshness result, source URI, vintage when applicable, and a decision trace ID.

The central quality engine rejects stale evidence and evidence whose receipt is older than policy. Numeric observations for the same instrument, value, and event date are compared across providers. A relative difference above policy is retained as a visible disagreement and vetoes the bundle; the values are never averaged. Broker truth is still rechecked independently immediately before any future order submission.

## Rollback

Disable the affected provider independently. Catalyst-dependent proposals abstain if Finnhub is unavailable. yfinance degradation does not interrupt reconciliation or exits. Disabling both restores Alpaca-only evidence without changing the broker or execution boundary.
