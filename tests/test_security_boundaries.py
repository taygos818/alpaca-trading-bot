import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_isolated_and_paper_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "name: alpaca-trading-bot-paper" in compose
    assert "https://paper-api.alpaca.markets" in compose
    assert "https://api.alpaca.markets" not in compose
    assert 'ALPACA_PAPER_TRADE: "true"' in compose
    assert "BOT_ENVIRONMENT: live" not in compose
    assert "TRADING_LANE: options_live" not in compose
    assert "ALPACA_LIVE_" not in compose
    assert "PAPER_ORDER_SUBMISSION_ENABLED: ${PAPER_ORDER_SUBMISSION_ENABLED:-false}" in compose
    assert "AGENT_COORDINATOR_ENABLED: ${AGENT_COORDINATOR_ENABLED:-false}" in compose
    assert "AGENT_COORDINATOR_SHADOW_MODE: ${AGENT_COORDINATOR_SHADOW_MODE:-true}" in compose
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FINNHUB_ENABLED=false" in env_example
    assert "YFINANCE_ENABLED=false" in env_example
    assert "FRED_ENABLED=false" in env_example
    assert "FEATHERLESS_ANALYSIS_ENABLED=false" in env_example
    assert "${FEATHERLESS_ANALYSIS_ENABLED" not in compose
    assert "ALPACA_ORDER_DRY_RUN: ${PAPER_ORDER_DRY_RUN:-true}" in compose
    assert "alpaca_agent_postgres_data" in compose
    assert "alpaca_agent_redis_data" in compose
    assert "127.0.0.1:${PAPER_POSTGRES_PORT:-56433}:5432" in compose
    assert "127.0.0.1:${PAPER_REDIS_PORT:-57380}:6379" in compose


def test_mcp_is_paper_only_local_and_profile_gated():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    mcp_section = compose.split("  alpaca-mcp:", 1)[1].split("  scheduler:", 1)[0]

    assert 'profiles: ["research"]' in mcp_section
    assert 'ALPACA_PAPER_TRADE: "true"' in mcp_section
    assert "127.0.0.1:${ALPACA_MCP_HOST_PORT:-8181}:8080" in mcp_section
    assert ".env.paper.secrets" in mcp_section


def test_obsolete_ai_modules_and_live_artifacts_are_absent():
    assert not (ROOT / "services/strategy-engine/ai_committee.py").exists()
    assert not (ROOT / "services/strategy-engine/ai_market_analyzer.py").exists()
    assert not (ROOT / "scripts/live_canary_activation.py").exists()
    assert not (ROOT / "scripts/live_liquidation_monitor.py").exists()
    assert not (ROOT / "LIVE_SETUP.md").exists()


def test_agent_coordinator_is_credential_free_and_cli_gateway_is_narrow():
    agent_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services/strategy-engine/multi_agent").glob("*.py")
    )
    gateway_source = (ROOT / "services/strategy-engine/execution_gateway/alpaca_cli.py").read_text(encoding="utf-8")
    assert "ALPACA_API_KEY" not in agent_source
    assert "ALPACA_SECRET_KEY" not in agent_source
    assert "subprocess" not in agent_source
    assert "shell=False" in gateway_source
    assert 'forbidden = {"api", "close-all", "cancel-all", "locate"}' in gateway_source
    assert "finnhub" not in gateway_source.lower()
    assert "yfinance" not in gateway_source.lower()
    assert "fred" not in gateway_source.lower()
    assert "featherless" not in gateway_source.lower()


def test_featherless_secret_is_confined_to_narrow_provider_adapter():
    coordinator_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services/strategy-engine/multi_agent").glob("*.py")
    )
    ai_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services/strategy-engine/ai_analysis").glob("*.py")
    )
    assert "FEATHERLESS_API_KEY" not in coordinator_source
    assert "ALPACA_API_KEY" not in ai_source
    assert "ALPACA_SECRET_KEY" not in ai_source
    assert "execution_gateway" not in ai_source


def test_defined_risk_options_is_default_off_and_cannot_execute():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    option_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services/strategy-engine/defined_risk_options").glob("*.py")
    )

    assert "DEFINED_RISK_OPTIONS_ENABLED=false" in env_example
    assert "ALPACA_API_KEY" not in option_source
    assert "ALPACA_SECRET_KEY" not in option_source
    assert "subprocess" not in option_source
    assert "execution_gateway" not in option_source


def test_paper_promotion_has_no_credentials_and_submission_requires_three_gates():
    runtime_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services/strategy-engine/paper_runtime").glob("*.py")
    )
    assert "ALPACA_API_KEY" not in runtime_source
    assert "ALPACA_SECRET_KEY" not in runtime_source
    assert "subprocess" not in runtime_source
    assert "PAPER_ORDER_SUBMISSION_ENABLED" in runtime_source
    assert "PAPER_ORDER_DRY_RUN" in runtime_source
    assert "M6_BOUNDED_SUBMISSION_ACK" in runtime_source


def test_default_test_run_cannot_load_local_secret_file():
    source = (ROOT / "tests/test_integration_feeds.py").read_text(encoding="utf-8")
    assert ".env.secrets" not in source
    assert "RUN_LIVE_INTEGRATION_TESTS" in source


def test_tracked_files_do_not_contain_alpaca_key_material():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode().split("\0")
    alpaca_key_id = re.compile(r"\bAK[A-Z0-9]{20,}\b")
    alpaca_secret = re.compile(r"\b[A-Za-z0-9]{35,}\b")
    findings = []
    for relative_path in filter(None, tracked):
        path = ROOT / relative_path
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if alpaca_key_id.search(content):
            findings.append(f"possible Alpaca key ID in {relative_path}")
        for line_number, line in enumerate(content.splitlines(), start=1):
            secret_assignment = re.search(r"(?:SECRET|API_KEY)[A-Z0-9_]*\s*[:=]", line.upper())
            if secret_assignment and alpaca_secret.search(line) and "replace" not in line.lower():
                findings.append(f"possible secret in {relative_path}:{line_number}")
    assert findings == []
