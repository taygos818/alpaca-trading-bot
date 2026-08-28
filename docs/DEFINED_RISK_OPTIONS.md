# Defined-Risk Options Strategy

Milestone 5 turns dynamic discovery plus frozen technical, catalyst, and macro analyses into deterministic options proposals. It contains no ticker allowlist. Every ranked candidate supplied by broad-market discovery is eligible to be evaluated under the same rules.

The configured Alpaca paper account was verified on 2026-08-28 as options approval Level 3 and trading Level 3, so its primary structure is a call debit spread for bullish candidates or put debit spread for bearish candidates. A Level 2 account automatically falls back to a long call or long put; Level 0 or 1 abstains.

## Entry rules

- Require a source-attributed catalyst and a completed underlying bar, both cited by independent analyses.
- Require technical and catalyst direction to agree at or above the configured confidence threshold. Macro may agree or remain neutral; opposition or any AI abstention blocks the proposal.
- Accept any discovered symbol, but only active, tradable contracts from 7 through 21 DTE.
- Require quotes from the single configured feed, positive two-sided markets, quote size, volume, open interest, and per-leg spread within policy. Feed identity is persisted in the proposal rationale.
- Target approximately 0.55 absolute delta for the purchased leg and 0.30 for the sold leg.
- Require same right, expiration, and ratio; calls buy the lower strike and puts buy the higher strike.
- Reject a net debit at or above spread width and require minimum reward-to-premium-risk.
- Use limit orders only. Risk uses the conservative observable natural debit, not an optimistic midpoint.

The configured contest account was checked on 2026-08-28: OPRA returned HTTP 403, while Alpaca's free `indicative` feed returned data. Alpaca documents that indicative trades are delayed and quotes are modified. Because this repository is permanently paper-only, the contest configuration explicitly uses `OPTIONS_MARKET_DATA_FEED=indicative`, conservative natural-debit limits, and the same liquidity gates. It never labels that data OPRA. A future OPRA subscription can switch the value to `opra`; this paper-only exception must not be copied into live trading.

## Deterministic risk

Options are sized in whole contracts. Quantity is the minimum capacity allowed by per-trade premium risk, total reserved maximum loss, per-underlying concentration, correlation-group exposure, options buying power, and cash after the configured buffer. The strategy also blocks on daily loss, open-position count, and pending-order count.

The proposal contract itself permits only a long option or a two-leg debit spread. It rejects uncovered legs, mismatched expirations or ratios, wrong call/put direction, and maximum loss below `limit_debit * 100 * quantity` before deterministic portfolio authorization or the CLI gateway can see the proposal.

## Strategy-owned exits

On a verified fill, the exit-plan factory binds proposal ID, exact legs, filled whole-contract quantity, actual entry debit, maximum premium loss, opening time, expiration, and thesis evidence. The append-only JSONL journal restores the newest state per plan after restart.

Exit assessment is deterministic and triggers on any of:

- 50% gain in spread or option value;
- 40% loss in value;
- three calendar days held;
- two or fewer days to expiration;
- thesis invalidation.

The strategy intentionally exits before expiration rather than relying on exercise or assignment. Alpaca begins expiration-day risk handling and can auto-exercise in-the-money contracts, so expiration control is an entry and lifecycle invariant, not an optional warning.

## Safety state and rollback

`DEFINED_RISK_OPTIONS_ENABLED=false` is the default. Milestone 5 creates proposals and exit plans but does not start the runtime, enable paper submission, or place an order. Disable the flag to stop new entries; retain reconciliation and persisted exit ownership for existing paper positions.
