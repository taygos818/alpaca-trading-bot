# Contest Architecture

## Objective

Autonomously discover, evaluate, structure, execute, and manage defined-risk options opportunities in Alpaca paper trading while retaining deterministic risk vetoes and a complete decision audit.

## Agent topology

```text
Alpaca + Finnhub + yfinance + FRED
                 |
        Data Quality Agent
                 |
       Frozen Evidence Bundle
                 |
   +-------------+-------------+
   |             |             |
Technical     Catalyst       Macro
 Agent          Agent        Agent
   +-------------+-------------+
                 |
       Options Structure Agent
                 |
       Adversarial Review Agent
                 |
   Deterministic Portfolio Risk Agent
                 |
      Immutable Authorization
                 |
       Alpaca CLI Execution Agent
                 |
        Alpaca Paper Account
                 |
 Order and Position Reconciliation Agent
```

Agents work independently at the analysis and proposal stages. They never execute independently. Only the execution adapter receives paper-order capability, and it accepts only a validated authorization record.

## Primary strategy

Catalyst-confirmed directional call and put debit spreads:

- Dynamic options-enabled universe; no fixed opportunity allowlist.
- Completed underlying bars and fresh option quotes only.
- Approximately 7 to 21 days to expiration.
- Liquid long and short legs selected using delta, bid/ask spread, volume, open interest, and maximum debit.
- Known maximum loss before authorization.
- Limit orders only for options entries and exits.
- Exit on profit target, loss limit, thesis invalidation, time limit, or approaching expiration.
- No uncovered short options and no intentional expiration exposure.

If the paper account supports only Level 2, the bounded fallback is a long call or put. It must follow the same premium-at-risk and lifecycle controls.

## AI boundary

Featherless-hosted models consume a frozen, source-attributed evidence bundle and return versioned structured JSON. AI output may rank evidence, identify contradictions, propose a direction, select among eligible structures, or abstain. It cannot:

- change risk limits;
- invent missing prices or Greeks;
- issue shell commands;
- access broker credentials;
- submit, cancel, replace, exercise, or close an order directly;
- bypass stale-data, liquidity, market-hours, exposure, or drawdown gates.

Invalid output, timeout, disagreement beyond configured tolerance, or unavailable required evidence results in no new trade.

## Alpaca tool requirement

The runtime execution boundary uses the Alpaca CLI with structured JSON, a pinned version, paper profile enforcement, dry-run previews, and deterministic client order IDs. The Alpaca MCP server is available as an optional paper-only research and demonstration profile.
