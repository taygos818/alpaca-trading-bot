# Agent Decision Dashboard

Milestone 7 adds a credential-free, read-only decision console at `/agents`. It reconstructs the newest persisted revision of every decision trace and never invents demonstration trades when the journal is empty.

The console shows:

- ranked opportunities when supplied by discovery metadata;
- each independent agent's direction, confidence, disposition, thesis, contradictions, and citation count;
- selected long options or debit spreads, exact legs, limit debit, quantity, and maximum loss;
- deterministic approvals, reductions, vetoes, and rejection reasons;
- reconciled order status without broker account identifiers;
- position quantity, mark, P&L, and strategy-owned exit status;
- evidence provider, instrument, event type, authority, freshness, and entitlement;
- the immutable replay fingerprint used to reconstruct the decision.

The dashboard container receives no Alpaca, Featherless, Finnhub, Robinhood, database, or Redis credentials. It mounts the strategy log directory read-only and exposes no start, stop, submit, cancel, replace, exercise, or close route. Stopping or rolling back `monitor-dash` cannot affect order reconciliation or position management.

Local URL: `http://127.0.0.1:8090/agents` by default. Override only the loopback port with `PAPER_DASHBOARD_PORT`.
