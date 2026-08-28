# Replay and Bounded Paper Launch

Milestone 6 promotes a credential-free coordinator result through a narrow Alpaca CLI boundary. It does not weaken the deterministic proposal, adversarial-review, or portfolio-risk contracts.

## Release gates

1. Historical and frozen scenarios use a completed signal bar and a later completed execution bar. Two identical replays must produce the same `DecisionTrace.replay_fingerprint`.
2. Current discovery, account, clock, position, open-order, option-contract, and option-snapshot reads contain no mock fallback.
3. The pinned CLI previews the exact typed order with `--dry-run`.
4. Paper submission requires all of `PAPER_ORDER_SUBMISSION_ENABLED=true`, `PAPER_ORDER_DRY_RUN=false`, and `M6_BOUNDED_SUBMISSION_ACK=paper-contest`.
5. Each process may submit at most one entry by default, with maximum authorized loss of $150 and at most four existing open orders.
6. The launcher checks the deterministic client order ID before checking open orders or submitting. A previous fill therefore remains idempotent after restart.
7. Every submitted order is immediately reconciled into a typed order event. Partial fills create position assessments and persisted exit ownership for the filled quantity only.
8. Exits use an `AuthorizedExit` that exactly reverses every persisted leg. The CLI exposes only single-order cancellation and position-reducing limit exits; bulk cancellation and generic API calls remain forbidden.

## Current validation

On 2026-08-28, the configured paper account was active at options Level 3 with zero positions and zero open orders. The market clock was closed until 2026-08-31 09:30 ET, so no strategy submission was appropriate. Read-only discovery returned 71 qualified candidates from 14,271 assets. One dynamically selected candidate returned 192 option contracts, 192 snapshots, and 69 normalized contracts across the configured 7–21 DTE indicative feed. A quantity-one, maximum-loss-below-$150 synthetic CLI transport preview passed with submission disabled. The synthetic preview is test evidence, not a strategy recommendation.

The strategy container now runs `contest_agent.py`, not the inherited wheel entrypoint. It consumes the current dynamic shortlist, a completed Alpaca minute bar, source-attributed Finnhub catalysts, optional yfinance and FRED research, three independent Featherless analyses, the defined-risk structure selector, deterministic allocation, the CLI preview/promotion boundary, reconciliation, exit-plan persistence, and decision journaling. When the market is closed it performs no research calls or entry work. Local paper configuration enables this path in shadow/dry-run mode; submission remains separately gated.

## Persistence and restart

`DECISION_TRACE_PATH` is an append-only JSONL journal. Each revision contains the complete evidence bundle, independent analyses, proposals, objections, risk decisions, commands, broker events, position assessments, replay fingerprint, and non-sensitive display metadata. The latest revision per trace is authoritative for the read-only dashboard.

Filled quantities also create append-only exit plans. Restart recovery uses deterministic client order IDs, broker reconciliation, and the newest exit-plan state. An unknown broker response, provider outage, stale input, malformed payload, or mismatched client ID fails closed.

## Rollback

Set `DEFINED_RISK_OPTIONS_ENABLED=false` and `PAPER_ORDER_SUBMISSION_ENABLED=false` to stop entries. Cancel only explicitly identified eligible paper orders, continue reconciling submitted orders, and retain exit management for filled positions. `PAPER_ORDER_DRY_RUN=true` returns the runtime to preview-only mode without touching the separate live repository.
