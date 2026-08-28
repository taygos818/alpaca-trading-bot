# Rollback Policy

Every milestone is feature-flagged or isolated so it can be removed without widening execution authority.

For any paper-execution anomaly:

1. Disable new entries and set submission back to dry-run.
2. Reconcile Alpaca orders, fills, positions, and persisted ownership.
3. Preserve evidence, logs, and database state.
4. Cancel only verified eligible paper orders.
5. Continue or explicitly close existing paper positions under their persisted exit plans.
6. Revert to the last validated commit and rerun startup, replay, and reconciliation checks.

Rollback never operates on the separate `trading-bot-us` repository or its live services.
