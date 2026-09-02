# Final-Day Aggressive Paper Profile

This paper-only profile is intentionally optimized for the short Alpaca competition window ending September 3, 2026. It accepts a high probability of substantial loss in exchange for materially larger defined-risk options exposure. It never enables live trading, naked options, duplicate submissions, or unowned exits.

## Active profile

- 1–14 DTE options, including near-expiration contracts before the forced competition flatten.
- Up to 25 contracts per proposal and $10,000 authorized maximum loss per order.
- Sizing budgets: 8% liquid, 6% expensive, 4% illiquid, with a 10% absolute per-trade cap.
- Portfolio limits: 85% total defined maximum loss, 30% per underlying, 60% per correlation group, 15 open positions, and 12 pending orders.
- No cash buffer, a 35% daily-loss circuit breaker, 50% profit target, and 50% premium-loss exit for newly filled positions.
- One-signal thesis-invalidation exits disabled; deterministic profit, premium-loss, holding-time, expiration, and September 3 forced-flatten exits remain active.
- Future-stamped observed Finnhub news is discarded rather than invalidating an otherwise valid candidate.

## Rollback

For immediate risk reduction without abandoning owned positions, set `PAPER_ENTRY_SUBMISSION_ENABLED=false` and recreate only `strategy-engine`; keep `PAPER_ORDER_SUBMISSION_ENABLED=true` so reconciliation and exits continue.

To restore the preceding conservative profile, revert commit `fe1c2d6`'s successor and rebuild `strategy-engine`, or override the `CONTEST_OPTIONS_*`, `PAPER_*`, and `M6_*` values with the prior 10-contract, 5% per-trade, 50% total-risk configuration. Never delete the persisted pending-entry, exit-plan, exit-order, or submission journals during rollback.
