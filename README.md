# Alpaca Trading Bot

An autonomous, multi-agent options trading system for the 2026 Alpaca AI Trading Agents Hackathon. This repository is intentionally restricted to Alpaca paper trading.

## Safety boundary

- The only accepted trading endpoint is `https://paper-api.alpaca.markets`.
- Paper mode and broker-state reconciliation are mandatory.
- Order submission starts disabled; dry-run is the default.
- AI agents may research and propose trades but cannot bypass deterministic portfolio risk controls or construct arbitrary broker commands.
- Never copy credentials, logs, databases, or runtime artifacts from another trading project.

## Competition strategy

The planned primary strategy uses catalyst-confirmed directional call and put debit spreads. A Level 2 paper account may use defined-risk long calls and puts until Level 3 spreads are available. The opportunity universe is dynamic and filtered for options eligibility and liquidity.

See [Contest Architecture](docs/CONTEST_ARCHITECTURE.md), [Decision Contracts](docs/DECISION_CONTRACTS.md), [Multi-Agent Coordinator](docs/MULTI_AGENT_COORDINATOR.md), [Milestones](docs/MILESTONES.md), [Data Provider Plan](docs/DATA_PROVIDER_PLAN.md), [External Data Provenance](docs/EXTERNAL_DATA_PROVENANCE.md), [Featherless Autonomous Analysis](docs/FEATHERLESS_ANALYSIS.md), and [Defined-Risk Options Strategy](docs/DEFINED_RISK_OPTIONS.md).

## Bootstrap

1. Copy `.env.example` to `.env.paper.secrets`.
2. Add only Alpaca paper credentials and a Featherless API key.
3. Keep `PAPER_ORDER_DRY_RUN=true` and `PAPER_ORDER_SUBMISSION_ENABLED=false`.
4. Validate configuration with `docker compose config`.
5. Run tests before starting any service.

Paper-order execution will remain disabled until its milestone acceptance gates pass.

## Provenance

The initial safety, reconciliation, persistence, and market-data architecture was derived from the owner's validated `trading-bot-us` commit `975aa8bbc3a4de674fa28912bc998e2da00bfa2a`. Git history, live-only deployment configuration, runtime data, secrets, and dormant AI modules were not copied.

## License

MIT. See [LICENSE](LICENSE).
