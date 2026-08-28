"""Narrow adapter for Alpaca's agent-oriented CLI.

This is the only Milestone 2 module allowed to receive Alpaca credentials or
spawn the CLI. It accepts typed execution contracts, constructs arguments
without a shell, forces paper routing, and exposes no raw API escape hatch or
bulk cancel/close operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Any, Callable

from agent_contracts import AuthorizedExecution, ExecutionAction, ExecutionCommand, LegSide, OptionRight


ALPACA_CLI_VERSION = "0.0.14"
ALPACA_CLI_COMMIT = "53606273aa230a40c64b783425dcb3f4423ede30"
ALLOWED_BINARY = "/usr/local/bin/alpaca"


class AlpacaCliError(RuntimeError):
    pass


class SubmissionDisabled(AlpacaCliError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class PaperCredentials:
    api_key: str
    secret_key: str

    def __post_init__(self) -> None:
        if not self.api_key or not self.secret_key:
            raise AlpacaCliError("complete paper credentials are required")


@dataclass(frozen=True, slots=True)
class GatewayPolicy:
    shadow_mode: bool = True
    submission_enabled: bool = False
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise AlpacaCliError("CLI timeout must be between 1 and 120 seconds")
        if self.shadow_mode and self.submission_enabled:
            raise AlpacaCliError("shadow mode and submission cannot both be enabled")


@dataclass(frozen=True, slots=True)
class CliResponse:
    operation: str
    payload: Any
    exit_code: int


Runner = Callable[..., subprocess.CompletedProcess[str]]
CredentialProvider = Callable[[], PaperCredentials]


class AlpacaCliGateway:
    """Allowlisted command gateway; never accepts caller-provided argv."""

    def __init__(
        self,
        credential_provider: CredentialProvider,
        *,
        policy: GatewayPolicy | None = None,
        binary: str = ALLOWED_BINARY,
        runner: Runner = subprocess.run,
    ) -> None:
        if binary != ALLOWED_BINARY:
            raise AlpacaCliError(f"CLI binary must be {ALLOWED_BINARY}")
        self._credential_provider = credential_provider
        self._policy = policy or GatewayPolicy()
        self._binary = binary
        self._runner = runner

    @property
    def policy(self) -> GatewayPolicy:
        return self._policy

    def verify_version(self) -> str:
        completed = self._invoke(("version",), api_command=False)
        version_text = completed.stdout.strip()
        if ALPACA_CLI_VERSION not in version_text:
            raise AlpacaCliError(
                f"unexpected Alpaca CLI version; required {ALPACA_CLI_VERSION} "
                f"from {ALPACA_CLI_COMMIT}"
            )
        return version_text

    def account(self) -> CliResponse:
        return self._json_call("account.get", ("account", "get", "--quiet"))

    def clock(self) -> CliResponse:
        return self._json_call("clock", ("clock", "--quiet"))

    def positions(self) -> CliResponse:
        return self._json_call("position.list", ("position", "list", "--quiet"))

    def open_orders(self) -> CliResponse:
        return self._json_call("order.list", ("order", "list", "--status", "open", "--quiet"))

    def order_by_client_id(self, client_order_id: str) -> CliResponse:
        self._require_identifier("client_order_id", client_order_id)
        return self._json_call(
            "order.get_by_client_id",
            ("order", "get-by-client-id", "--client-order-id", client_order_id, "--quiet"),
        )

    def preview(self, execution: AuthorizedExecution) -> CliResponse:
        command = self._validated_command(execution)
        args = (*self._order_args(command), "--dry-run", "--quiet")
        return self._json_call("order.preview", args)

    def submit(self, execution: AuthorizedExecution) -> CliResponse:
        if self._policy.shadow_mode:
            raise SubmissionDisabled("shadow mode cannot submit orders")
        if not self._policy.submission_enabled:
            raise SubmissionDisabled("paper order submission is disabled")
        command = self._validated_command(execution)
        return self._json_call("order.submit", (*self._order_args(command), "--quiet"))

    @staticmethod
    def _validated_command(execution: AuthorizedExecution) -> ExecutionCommand:
        if not isinstance(execution, AuthorizedExecution):
            raise AlpacaCliError("gateway requires an AuthorizedExecution envelope")
        return execution.command

    def _order_args(self, command: ExecutionCommand) -> tuple[str, ...]:
        if not isinstance(command, ExecutionCommand):
            raise AlpacaCliError("execution requires a validated ExecutionCommand")
        if command.action is not ExecutionAction.SUBMIT:
            raise AlpacaCliError("Milestone 2 gateway supports typed submit commands only")
        if not command.legs:
            raise AlpacaCliError("option order requires at least one leg")
        self._validate_defined_risk_shape(command)

        common = (
            "order",
            "submit",
            "--qty",
            str(command.quantity),
            "--type",
            "limit",
            "--limit-price",
            format(command.limit_price, "f"),
            "--time-in-force",
            "day",
            "--client-order-id",
            command.client_order_id,
        )
        if len(command.legs) == 1:
            leg = command.legs[0]
            return (
                *common,
                "--symbol",
                leg.option_symbol,
                "--side",
                leg.side.value,
                "--position-intent",
                "buy_to_open" if leg.side is LegSide.BUY else "sell_to_open",
            )

        legs = [
            {
                "symbol": leg.option_symbol,
                "ratio_qty": str(leg.quantity),
                "side": leg.side.value,
                "position_intent": "buy_to_open" if leg.side is LegSide.BUY else "sell_to_open",
            }
            for leg in command.legs
        ]
        return (
            *common,
            "--order-class",
            "mleg",
            "--legs",
            json.dumps(legs, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _validate_defined_risk_shape(command: ExecutionCommand) -> None:
        if len(command.legs) == 1:
            if command.legs[0].side is not LegSide.BUY:
                raise AlpacaCliError("single-leg option entry must be long-only")
            return
        if len(command.legs) != 2:
            raise AlpacaCliError("Milestone 2 permits only long options or two-leg debit spreads")
        bought = [leg for leg in command.legs if leg.side is LegSide.BUY]
        sold = [leg for leg in command.legs if leg.side is LegSide.SELL]
        if len(bought) != 1 or len(sold) != 1:
            raise AlpacaCliError("debit spread requires exactly one bought and one sold leg")
        long_leg, short_leg = bought[0], sold[0]
        if (
            long_leg.right is not short_leg.right
            or long_leg.expiration != short_leg.expiration
            or long_leg.quantity != short_leg.quantity
        ):
            raise AlpacaCliError("debit-spread legs must share right, expiration, and ratio")
        is_call_debit = long_leg.right is OptionRight.CALL and long_leg.strike < short_leg.strike
        is_put_debit = long_leg.right is OptionRight.PUT and long_leg.strike > short_leg.strike
        if not (is_call_debit or is_put_debit):
            raise AlpacaCliError("option legs do not form a defined-risk debit spread")

    def _json_call(self, operation: str, args: tuple[str, ...]) -> CliResponse:
        completed = self._invoke(args, api_command=True)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AlpacaCliError(f"{operation} returned invalid JSON") from exc
        return CliResponse(operation=operation, payload=payload, exit_code=completed.returncode)

    def _invoke(self, args: tuple[str, ...], *, api_command: bool) -> subprocess.CompletedProcess[str]:
        forbidden = {"api", "close-all", "cancel-all", "locate"}
        if any(argument in forbidden for argument in args):
            raise AlpacaCliError("command is outside the allowlisted CLI surface")
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "ALPACA_LIVE_TRADE": "false",
            "ALPACA_OUTPUT": "json",
            "ALPACA_QUIET": "true",
        }
        if api_command:
            credentials = self._credential_provider()
            environment["ALPACA_API_KEY"] = credentials.api_key
            environment["ALPACA_SECRET_KEY"] = credentials.secret_key
        try:
            completed = self._runner(
                (self._binary, *args),
                env=environment,
                capture_output=True,
                text=True,
                timeout=self._policy.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AlpacaCliError(f"Alpaca CLI invocation failed: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            detail = self._safe_error(completed.stderr)
            raise AlpacaCliError(f"Alpaca CLI exited {completed.returncode}: {detail}")
        if api_command and not completed.stdout.strip():
            raise AlpacaCliError("Alpaca CLI returned an empty response")
        return completed

    @staticmethod
    def _safe_error(stderr: str) -> str:
        try:
            payload = json.loads(stderr)
            status = payload.get("status", "unknown")
            code = payload.get("code", "unknown")
            return f"structured CLI error status={status} code={code}"
        except (json.JSONDecodeError, AttributeError):
            return "CLI error details withheld"

    @staticmethod
    def _require_identifier(name: str, value: str) -> None:
        if not value or len(value) > 128 or not all(character.isalnum() or character in "._:-" for character in value):
            raise AlpacaCliError(f"invalid {name}")
