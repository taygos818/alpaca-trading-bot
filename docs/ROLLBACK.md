# Rollback Policy

Every milestone is feature-flagged or isolated so it can be removed without widening execution authority.

For any paper-execution anomaly:

1. Disable new entries and set submission back to dry-run.
2. Reconcile Alpaca orders, fills, positions, and persisted ownership.
3. Preserve evidence, logs, and database state.
4. Cancel only verified eligible paper orders.
5. Continue or explicitly close existing paper positions under their persisted exit plans.
6. Revert to the last validated commit and rerun startup, replay, and reconciliation checks.

For the contest options strategy, set `DEFINED_RISK_OPTIONS_ENABLED=false` to stop new proposals. This does not delete active exit plans. Position reconciliation and deterministic exit assessment continue until every paper position is closed or explicitly handed off.

The `monitor-dash` service is observation-only. It can be stopped or reverted independently because it has no credentials, writable strategy volume, or execution route.

Rollback never operates on the separate `trading-bot-us` repository or its live services.
