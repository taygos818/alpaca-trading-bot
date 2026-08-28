from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import (  # noqa: E402
    AuthorizedExecution,
    ContractValidationError,
    Direction,
    ExecutionAction,
    ExecutionCommand,
    LegSide,
    OptionLeg,
    OptionRight,
    OptionsProposal,
    ProposalDecision,
    RiskAuthorization,
    RiskDecision,
    contract_fingerprint,
)
from execution_gateway import (  # noqa: E402
    ALPACA_CLI_COMMIT,
    ALPACA_CLI_VERSION,
    AlpacaCliError,
    AlpacaCliGateway,
    GatewayPolicy,
    PaperCredentials,
    SubmissionDisabled,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


class RecordingRunner:
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def authorized_execution(*, multi_leg=True, single_leg_side=LegSide.BUY):
    legs = (
        OptionLeg("AAPL260904C00230000", LegSide.BUY, OptionRight.CALL, 1, Decimal("230"), date(2026, 9, 4)),
        OptionLeg("AAPL260904C00235000", LegSide.SELL, OptionRight.CALL, 1, Decimal("235"), date(2026, 9, 4)),
    )
    if not multi_leg:
        original = legs[0]
        legs = (
            OptionLeg(
                original.option_symbol,
                single_leg_side,
                original.right,
                original.quantity,
                original.strike,
                original.expiration,
            ),
        )
    proposal = OptionsProposal(
        record_id="proposal.001",
        trace_id="trace.001",
        evidence_bundle_id="bundle.001",
        analysis_ids=("analysis.001",),
        underlying="AAPL",
        decision=ProposalDecision.PROPOSE,
        direction=Direction.BULLISH,
        strategy_name="call_debit_spread" if multi_leg else "long_call",
        legs=legs,
        contract_quantity=1,
        limit_debit=Decimal("1.25"),
        maximum_loss=Decimal("125"),
        rationale="Test-defined-risk option entry.",
        created_at=NOW,
    )
    authorization = RiskAuthorization(
        record_id="authorization.001",
        trace_id="trace.001",
        proposal_id=proposal.record_id,
        proposal_fingerprint=contract_fingerprint(proposal),
        objection_ids=(),
        decision=RiskDecision.APPROVE,
        authorized_quantity=1,
        authorized_maximum_loss=Decimal("125"),
        reason="Within test limits.",
        expires_at=NOW.replace(minute=2),
        created_at=NOW,
    )
    command = ExecutionCommand(
        record_id="command.001",
        trace_id="trace.001",
        authorization_id="authorization.001",
        authorization_fingerprint=contract_fingerprint(authorization),
        proposal_id="proposal.001",
        action=ExecutionAction.SUBMIT,
        client_order_id="agent.trace.001",
        legs=legs,
        quantity=1,
        limit_price=Decimal("1.25"),
        created_at=NOW,
    )
    return AuthorizedExecution(proposal, authorization, command)


def credentials():
    return PaperCredentials("paper-key-value", "paper-secret-value")


def test_preview_builds_structured_multileg_cli_without_shell():
    runner = RecordingRunner(stdout='{"dry_run":true}')
    gateway = AlpacaCliGateway(credentials, runner=runner)
    response = gateway.preview(authorized_execution())

    args, kwargs = runner.calls[0]
    assert args[:3] == ("/usr/local/bin/alpaca", "order", "submit")
    assert "--dry-run" in args
    assert args[args.index("--order-class") + 1] == "mleg"
    legs = json.loads(args[args.index("--legs") + 1])
    assert [leg["side"] for leg in legs] == ["buy", "sell"]
    assert kwargs["shell"] is False
    assert kwargs["env"]["ALPACA_LIVE_TRADE"] == "false"
    assert kwargs["env"]["ALPACA_API_KEY"] == "paper-key-value"
    assert response.operation == "order.preview"
    assert response.payload == {"dry_run": True}


def test_single_leg_option_uses_typed_simple_limit_order():
    runner = RecordingRunner()
    AlpacaCliGateway(credentials, runner=runner).preview(authorized_execution(multi_leg=False))
    args, _ = runner.calls[0]
    assert "--order-class" not in args
    assert args[args.index("--symbol") + 1] == "AAPL260904C00230000"
    assert args[args.index("--position-intent") + 1] == "buy_to_open"
    assert args[args.index("--type") + 1] == "limit"


def test_shadow_mode_cannot_submit_and_never_invokes_runner():
    runner = RecordingRunner()
    gateway = AlpacaCliGateway(credentials, runner=runner)
    with pytest.raises(SubmissionDisabled, match="shadow mode"):
        gateway.submit(authorized_execution())
    assert runner.calls == []


def test_submission_requires_both_non_shadow_and_explicit_enable():
    runner = RecordingRunner()
    disabled = AlpacaCliGateway(
        credentials,
        policy=GatewayPolicy(shadow_mode=False, submission_enabled=False),
        runner=runner,
    )
    with pytest.raises(SubmissionDisabled, match="disabled"):
        disabled.submit(authorized_execution())

    enabled = AlpacaCliGateway(
        credentials,
        policy=GatewayPolicy(shadow_mode=False, submission_enabled=True),
        runner=runner,
    )
    enabled.submit(authorized_execution())
    assert "--dry-run" not in runner.calls[-1][0]


def test_gateway_exposes_only_allowlisted_operations_and_rejects_custom_binary():
    public_methods = {name for name in dir(AlpacaCliGateway) if not name.startswith("_")}
    assert public_methods == {
        "account",
        "clock",
        "open_orders",
        "order_by_client_id",
        "policy",
        "positions",
        "preview",
        "submit",
        "verify_version",
    }
    with pytest.raises(AlpacaCliError, match="CLI binary"):
        AlpacaCliGateway(credentials, binary="/bin/sh")


def test_cli_error_redacts_unstructured_stderr_and_credentials_repr():
    runner = RecordingRunner(returncode=1, stderr="paper-secret-value failed")
    gateway = AlpacaCliGateway(credentials, runner=runner)
    with pytest.raises(AlpacaCliError, match="details withheld") as error:
        gateway.account()
    assert "paper-secret-value" not in str(error.value)
    assert "paper-key-value" not in repr(credentials())
    assert "paper-secret-value" not in repr(credentials())


def test_cli_version_and_docker_build_are_pinned_to_verified_revision():
    runner = RecordingRunner(stdout=f"alpaca version {ALPACA_CLI_VERSION}\n")
    assert AlpacaCliGateway(credentials, runner=runner).verify_version().endswith(ALPACA_CLI_VERSION)
    dockerfile = (ENGINE / "Dockerfile").read_text(encoding="utf-8")
    assert ALPACA_CLI_COMMIT in dockerfile
    assert "@latest" not in dockerfile
    assert "install_alpaca_cli.py" in dockerfile
    installer = (ENGINE / "install_alpaca_cli.py").read_text(encoding="utf-8")
    assert "6c82ef31f94dd61aae1c90e40fc41fdfaf8111bd50e9a2780b9d8d304eb2ba66" in installer
    assert "621270e2b935dbae587e6ae05fe04a10bc178b4c9c638961a3d0214568ff2617" in installer


def test_invalid_cli_json_fails_closed():
    gateway = AlpacaCliGateway(credentials, runner=RecordingRunner(stdout="not-json"))
    with pytest.raises(AlpacaCliError, match="invalid JSON"):
        gateway.clock()


def test_gateway_rejects_command_without_authorization_envelope():
    gateway = AlpacaCliGateway(credentials, runner=RecordingRunner())
    with pytest.raises(AlpacaCliError, match="AuthorizedExecution"):
        gateway.preview(authorized_execution().command)


def test_gateway_rejects_uncovered_single_leg_sell():
    with pytest.raises(ContractValidationError, match="long-only"):
        authorized_execution(multi_leg=False, single_leg_side=LegSide.SELL)


def test_authorized_envelope_rejects_risk_that_widens_proposal():
    execution = authorized_execution()
    widened = RiskAuthorization(
        record_id="authorization.widened",
        trace_id=execution.proposal.trace_id,
        proposal_id=execution.proposal.record_id,
        proposal_fingerprint=contract_fingerprint(execution.proposal),
        objection_ids=(),
        decision=RiskDecision.APPROVE,
        authorized_quantity=2,
        authorized_maximum_loss=Decimal("250"),
        reason="Invalid widening.",
        expires_at=execution.authorization.expires_at,
        created_at=NOW,
    )
    with pytest.raises(ContractValidationError, match="exceeds proposal quantity"):
        AuthorizedExecution(execution.proposal, widened, execution.command)


def test_authorized_envelope_rejects_understated_option_premium_risk():
    execution = authorized_execution()
    with pytest.raises(ContractValidationError, match="understates premium at risk"):
        OptionsProposal(
            record_id="proposal.understated",
            trace_id=execution.proposal.trace_id,
            evidence_bundle_id=execution.proposal.evidence_bundle_id,
            analysis_ids=execution.proposal.analysis_ids,
            underlying=execution.proposal.underlying,
            decision=execution.proposal.decision,
            direction=execution.proposal.direction,
            strategy_name=execution.proposal.strategy_name,
            legs=execution.proposal.legs,
            contract_quantity=1,
            limit_debit=Decimal("1.25"),
            maximum_loss=Decimal("100"),
            rationale="Risk is intentionally understated for this test.",
            created_at=NOW,
        )
