# Multi-Agent Coordinator

Milestone 2 added a credential-free, shadow-only coordinator and a separate paper-only Alpaca CLI gateway. Milestone 4 can supply independently executed Featherless evaluators to the technical, catalyst, and macro analysis ports. The coordinator is disabled unless `AGENT_COORDINATOR_ENABLED=true`; its policy remains shadow-only until the bounded paper-submission milestone.

## Runtime boundaries

```text
Data Quality Agent
        |
  Frozen Evidence
        |
  +-----+--------+       independent concurrent analysis
  |     |        |
Tech  Catalyst  Macro
  +-----+--------+
        |
Options Structure Agents  independent concurrent proposals
        |
Adversarial Review Agent  blocking veto
        |
Portfolio Risk Agent      deterministic sole allocator
        |
Execution Agent           creates typed command only
        |
Shadow Preview Boundary   always adds --dry-run
        |
Alpaca CLI Gateway        separate credentialed module
```

The position-analysis agent consumes reconciled order events and produces typed position assessments. It does not create commands. Persistent broker reconciliation is introduced with the paper canary in Milestone 6.

## Alpaca CLI boundary

The [official Alpaca CLI](https://github.com/alpacahq/cli) warns that it is an alpha preview without pre-1.0 compatibility guarantees. The strategy-engine image therefore pins CLI `v0.0.14` at upstream commit `53606273aa230a40c64b783425dcb3f4423ede30`. Its installer selects Alpaca's Linux release for the build architecture and verifies the published SHA-256 digest before writing the binary.

The adapter:

- invokes a fixed `/usr/local/bin/alpaca` path with an argument tuple and `shell=False`;
- forces `ALPACA_LIVE_TRADE=false` and JSON output;
- accepts only self-validating `AuthorizedExecution` envelopes binding the proposal, non-rejected risk authorization, and command fingerprints;
- supports account, clock, positions, open orders, client-order-ID lookup, order preview, and explicitly gated submission;
- uses long-only simple limit orders for one option leg and defined-risk two-leg debit `mleg` limit orders with structured JSON for spreads;
- exposes no raw `alpaca api` call, locate request, bulk close, or bulk cancel;
- withholds unstructured CLI errors so credentials cannot leak through diagnostics.

## Determinism and idempotency

- Analysis and structure agents execute concurrently but their results are sorted by stable IDs before allocation.
- Stable proposal IDs cannot be reused with different content.
- Repeated identical cycles emit no second command.
- Deterministic client order IDs provide a broker-side duplicate boundary for later paper submission.
- The allocator processes proposals in a stable order, accounts for reserved maximum loss, caps total and per-underlying loss, controls position count, reduces whole-contract quantity when necessary, rejects opposing same-underlying directions, and honors adversarial vetoes.
- The execution envelope rejects any proposal or authorization that understates the limit debit multiplied by the 100-share options multiplier and authorized contract quantity.

## Current safety state

Milestones 2 through 4 do not start a service or submit a paper order. External data and Featherless analysis now terminate at typed analyses and shadow proposal gates. The contest options strategy, persistence, broker reconciliation, and bounded paper submission belong to later milestones.

Rollback is immediate: leave `AGENT_COORDINATOR_ENABLED=false` and `PAPER_ORDER_SUBMISSION_ENABLED=false`, or revert the Milestone 2 commit. The legacy paper engine and the separate live `trading-bot-us` project are not activated or modified by this coordinator.
