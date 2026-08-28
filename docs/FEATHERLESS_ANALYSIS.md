# Featherless Autonomous Analysis

Milestone 4 adds independent hosted-model research agents without giving AI execution authority. Featherless receives the same immutable, source-attributed evidence bundle for the technical, catalyst, and macro perspectives. Each response must be strict JSON, cite exact evidence record IDs, and either analyze or abstain.

## Local configuration

Put the API key only in the ignored `.env.paper.secrets` file:

```dotenv
FEATHERLESS_API_KEY=your-key-here
```

Do not place the key in `.env.example`, Compose YAML, source code, logs, issues, chat, or screenshots. `FEATHERLESS_ANALYSIS_ENABLED` defaults to `false`; adding a key alone does not activate paid inference. Compose passes this secrets file directly into the strategy container, so provider flags and model settings should also be changed there rather than exported in the shell.

The default registry entry is `Qwen/Qwen3-30B-A3B-Instruct-2507`, captured from Featherless's model catalog on 2026-08-28. It is an official Qwen instruct model with a 32K context window and one concurrency unit. A model ID outside the reviewed registry fails configuration instead of silently switching models.

## Trust boundary

The system prompt treats evidence text, headlines, URLs, and provider payloads as untrusted quoted data. AI cannot call tools, see Alpaca credentials, size exposure, modify portfolio limits, construct execution commands, or access the Alpaca CLI gateway. Unknown response fields—including prices, quantities, or order instructions—make the response invalid.

Exact citation validation prevents a response from referring to evidence outside the frozen bundle. Invalid JSON, unsupported claims without valid citations, timeout, HTTP failure, rate limit, missing credentials, or budget exhaustion stops proposal generation for that cycle. An explicit AI abstention becomes a neutral, zero-confidence analysis and deterministically prevents the options-structure stage from producing a proposal.

## Cost and audit controls

Before a paid request, a thread-safe guard reserves a conservative worst-case token and cost estimate. Defaults allow three requests per trace, 75,000 conservatively estimated total tokens, at most $0.05 per trace, and at most $2.50 per UTC day. The daily amount is intentionally a configuration ceiling, not a promise to spend it.

The JSONL audit contains the model and prompt versions, prompt and response hashes, provider request ID, result, token counts, estimated cost, error category, agent, trace, and timestamp. It never stores the API key, raw prompt, raw evidence, or raw model response.

## Rollback

Set `FEATHERLESS_ANALYSIS_ENABLED=false`. This removes AI proposal generation without changing the frozen evidence pipeline, deterministic portfolio risk authority, paper broker reconciliation, or execution gateway. Adding the key alone never enables analysis.
