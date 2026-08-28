"""Normalize Alpaca option contracts and snapshots into strict strategy inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from agent_contracts import OptionRight
from external_data.common import ProviderUnavailable, utc_datetime

from .models import OptionSnapshot


def normalize_alpaca_chain(
    underlying: str,
    contracts: tuple[dict, ...] | list[dict],
    snapshots: dict[str, dict],
    *,
    feed: str,
) -> tuple[OptionSnapshot, ...]:
    normalized = []
    for contract in contracts:
        symbol = str(contract.get("symbol") or "")
        snapshot = snapshots.get(symbol)
        if not symbol or not isinstance(snapshot, dict):
            continue
        if contract.get("tradable") is False or str(contract.get("status", "active")).lower() != "active":
            continue
        quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
        greeks = snapshot.get("greeks") or {}
        daily = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
        try:
            right = OptionRight(str(contract.get("type") or contract.get("right")).lower())
            item = OptionSnapshot(
                option_symbol=symbol,
                underlying=underlying.upper(),
                right=right,
                strike=Decimal(str(contract["strike_price"])),
                expiration=date.fromisoformat(str(contract["expiration_date"])),
                bid=Decimal(str(_first(quote, "bp", "bid_price"))),
                ask=Decimal(str(_first(quote, "ap", "ask_price"))),
                bid_size=int(_first(quote, "bs", "bid_size", default=0)),
                ask_size=int(_first(quote, "as", "ask_size", default=0)),
                volume=int(_first(daily, "v", "volume", default=0)),
                open_interest=int(_first(snapshot, "openInterest", "open_interest", default=0)),
                delta=Decimal(str(greeks["delta"])),
                quote_time=utc_datetime(_first(quote, "t", "timestamp")),
                feed=feed,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation, ProviderUnavailable):
            continue
        normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: (item.expiration, item.right.value, item.strike, item.option_symbol)))


def _first(mapping: dict, *keys: str, default=None):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default
