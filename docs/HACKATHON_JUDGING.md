# Taygos818 Alpaca Hackathon — Judging One-Pager

**Alpaca paper trading account ID:** `eb6ba33a-57ac-4abc-99f6-ae72a9c9b30f`  
**Competition window:** August 31–September 3, 2026  
**Repository:** `taygos818/alpaca-trading-bot`

## What the agent does

The bot is an autonomous, paper-only options system that searches a dynamic US-equity universe instead of relying on a fixed ticker allowlist. Alpaca supplies broker truth, option contracts, executable quotes, completed one-minute bars, and SIP-powered market discovery. Finnhub supplies source-attributed company news, while yfinance is optional, non-authoritative historical context. Every observation is normalized into a timestamped evidence record and frozen into an immutable bundle before analysis.

Three independent Featherless-hosted AI agents receive the same frozen bundle. The **technical agent** evaluates VWAP position, opening range, short-term trend, and volume confirmation. The **catalyst agent** assesses whether current news or unusual activity can persist during the session. The **macro agent** looks only for supplied broad-market evidence and defaults neutral when none applies. Each agent must return strict JSON, cite exact evidence IDs, and may abstain. Invalid, unsupported, stale, or contradictory output cannot become an order.

AI never sizes or submits trades. A deterministic strategy converts an agreed direction into a call or put debit spread using expiration, delta, bid/ask spread, quote age, volume, open interest, reward/risk, and known maximum debit. A separate adversarial reviewer can object, and the portfolio-risk allocator has final veto authority. Only a signed, immutable authorization reaches execution.

## Risk gates and contest profile

The permanent invariants are paper-only routing, completed bars, fresh executable quotes, defined maximum loss, no naked options, whole-contract sizing, duplicate-order prevention, deterministic client order IDs, broker reconciliation, and strategy-owned exits. Exposure is tracked by trade, underlying, correlation group, open positions, pending orders, cash, buying power, and daily drawdown. Filled and partially filled entries create durable exit plans that survive restarts.

For the final short competition window, the profile was deliberately aggressive: up to 25 contracts, a 10% per-trade cap, 85% total defined-risk exposure, 30% per underlying, 60% correlated exposure, 1–14 DTE, and a 35% daily-loss circuit breaker. New positions used 50% profit and premium-loss thresholds. Entries stopped at 3:15 p.m. ET on September 3, with a scheduled 3:45 p.m. ET forced flatten. This profile increased upside potential and, as the result shows, materially increased downside as well.

## Alpaca infrastructure

All trading runs through the pinned **Alpaca CLI**, satisfying the hackathon tool requirement. The gateway accepts a narrow allowlist of structured paper commands and rejects the live endpoint. It uses Alpaca paper account, clock, asset, position, order, option-contract, option-snapshot, and market-data responses; previews orders before promotion; submits limit multi-leg orders; and reconciles fills, cancellations, partial fills, and restarts. Redis supports bounded market-data caching, PostgreSQL supports isolated paper infrastructure, append-only JSONL journals preserve decisions and lifecycle state, and a credential-free dashboard displays heartbeats and trace drill-downs.

```text
Alpaca/Finnhub/yfinance → frozen evidence → 3 AI analysts
→ deterministic options structure → adversarial review → risk allocator
→ Alpaca CLI paper execution → order/position reconciliation → owned exits
```

## Final result and honest retrospective

The account began at **$100,000** and ended the September 3 snapshot at **$86,321.79**: **−$13,678.21 (−13.68%)**. Daily P&L was approximately **−$1,564.48**, **−$64.32**, **−$4,690.56**, and **−$7,358.85**. The system recorded 25,136 unique decision traces, made 8,677 Featherless requests (7,969 valid analyses), submitted 60 entries and 30 exits, and recorded 122 option-leg fills. Alpaca reported no rejected orders.

The experiment demonstrated autonomous discovery, independently reasoned AI decisions, deterministic risk authorization, real paper execution, and restart-safe lifecycle ownership—but not profitable performance. The largest weaknesses were noisy catalyst attribution, invalid/budget-limited AI responses, too many low-quality retries, crossing wide option spreads, and increasing size before proving positive expectancy. The forced-flatten process closed six plans, but nine spreads remained open after the close because unfilled limit exits were still being repriced when the market closed; the engine then stopped exit reconciliation. That is a confirmed lifecycle defect, not a claimed success.
