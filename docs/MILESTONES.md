# Delivery Milestones

## Milestone 0 — Isolated project bootstrap

Create the private repository, local checkout, Linear project, MIT license, paper-only Compose project, separate state, contest requirements, and source provenance. Remove live deployment configuration and dormant AI modules.

Acceptance: no secrets or runtime artifacts; no live endpoint or broker; dry-run default; Compose and security tests pass; existing live project is untouched.

Rollback: remove or archive only `alpaca-trading-bot` resources.

## Milestone 1 — Typed decision contracts

Create immutable, versioned schemas for evidence, agent analyses, options proposals, adversarial objections, risk authorizations, execution commands, order events, and position assessments.

Acceptance: invalid or untraceable output fails closed; replay reproduces deterministic decisions.

Rollback: keep contracts behind the coordinator feature flag.

## Milestone 2 — Alpaca CLI and multi-agent coordinator

Build a narrow, pinned CLI adapter plus independent data, technical, catalyst, macro, options-structure, adversarial, risk, execution, and position agents. Strategies can propose concurrently; one deterministic allocator authorizes exposure.

Acceptance: no arbitrary commands; no agent has credentials; shadow mode cannot submit; duplicate proposals remain idempotent.

Rollback: disable the coordinator and keep submission disabled.

## Milestone 3 — External data and provenance

Implement Finnhub, yfinance, FRED, and centralized data-quality milestones described in `DATA_PROVIDER_PLAN.md`.

Acceptance: entitlement, staleness, disagreement, outage, caching, provenance, and replay tests pass.

Rollback: independent provider flags; Alpaca remains broker truth.

## Milestone 4 — Featherless autonomous analysis

Implement a provider adapter, model registry, structured prompts, evidence citations, abstention, timeout handling, budget limits, and prompt/model audit. Agents independently analyze the same frozen evidence bundle.

Acceptance: AI materially affects proposals, but cannot modify risk or issue broker commands; invalid or unsupported claims yield no trade.

Rollback: disable AI proposal generation; no order path remains reachable.

## Milestone 5 — Defined-risk options strategy

Implement catalyst-confirmed call/put debit spreads with Level 2 long-option fallback, dynamic discovery, completed bars, liquid chains, known maximum loss, and strategy-owned exits.

Acceptance: no uncovered exposure; whole-contract sizing; limit orders; expiration controls; premium-at-risk, concentration, correlation, pending-order, and daily-loss gates pass.

Rollback: disable the options strategy and reconcile existing paper positions under their persisted exit plans.

## Milestone 6 — Replay, smoke validation, and paper launch

Progress through historical replay, frozen scenarios, live-market shadow, CLI dry-run, bounded paper submissions, autonomous lifecycle, restart tests, partial fills, and provider outages. These are deterministic same-session release gates, not multi-day canary or burn-in periods.

Acceptance: complete evidence-to-exit trace for every proposal and trade; no unresolved broker state.

Rollback: pause new entries, cancel eligible paper orders, retain position management, and return to dry-run.

Competition launch profile (August 31 through September 3, 2026): discovery retries begin at
09:30 ET; entries stop at 15:15 ET on September 3; deterministic exits force flattening by
15:45 ET. Accepted entries and exits are restart-safe, partial fills create owned exit plans,
and entry throughput is bounded by daily submissions, open orders, defined maximum loss,
portfolio exposure, and daily drawdown. Exit orders are never blocked by the entry-count fuse.

Rollback: set `PAPER_ENTRY_SUBMISSION_ENABLED=false`, rebuild the strategy engine, and
leave `PAPER_ORDER_SUBMISSION_ENABLED=true` so reconciliation plus deterministic exits
remain active for existing paper positions.

## Milestone 7 — Dashboard and explainability

Show agent findings, disagreements, opportunity rankings, chosen structures, risk decisions, rejected alternatives, positions, P&L, exits, and provenance without exposing credentials or sensitive account identifiers.

Acceptance: hosted demo is truthful, readable, and reconstructs each decision from persisted evidence.

Rollback: stop the dashboard without affecting paper reconciliation or position management.

## Milestone 8 — Contest submission

Prepare the public MIT repository, hosted URL, README, architecture visual, cover image, slides, video, descriptions, tags, and competition-window results.

Acceptance: secret, dependency-license, provenance, Git-history, link, and clean-install checks pass before repository visibility changes.

Rollback: keep the repository private and submit nothing until the release audit passes.
