import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import BaseStrategy, TradeIntent
from utils.data_feed import build_market_snapshot_provider
from utils.market_discovery import DiscoverySettings
from utils.storage import TradeStore


def current_time_et():
    return datetime.now(ZoneInfo("America/New_York"))


class OpeningOpportunityStrategy(BaseStrategy):
    """SIP-discovered candidates confirmed with a completed ORB and session VWAP."""

    name = "tier4_opening"

    def __init__(self):
        self.data = build_market_snapshot_provider()
        self.store = TradeStore.from_env() if os.getenv("POSTGRES_DSN", "").strip() else None
        self.path = Path(os.getenv("PREMARKET_DISCOVERY_OUTPUT_PATH", "/app/paper-data/premarket-watchlist.json"))
        self.discovery_config_hash = DiscoverySettings.from_env().config_hash()
        self.range_minutes = int(os.getenv("OPENING_RANGE_MINUTES", "15"))
        self.start_minute = int(os.getenv("OPENING_START_MINUTE_ET", "46"))
        self.end_minute = int(os.getenv("OPENING_END_MINUTE_ET", "75"))
        self.max_notional = float(os.getenv("OPENING_MAX_NOTIONAL", "1000"))
        self.min_volume = float(os.getenv("OPENING_MIN_CUMULATIVE_VOLUME", "50000"))
        self.max_bar_age = float(os.getenv("OPENING_MAX_BAR_AGE_SECONDS", "120"))
        self.shorts_enabled = os.getenv("OPENING_SHORTS_ENABLED", "false").strip().lower() == "true"
        self.trading_lane = os.getenv("TRADING_LANE", "stock_paper").strip().lower()
        self._last_owned_symbols = set()

    def _hold(self, nav, note, symbol="OPENING"):
        return TradeIntent(self.name, symbol, "hold", 0, 0.0, nav, notes=note)

    def _fresh(self, metrics: dict, now: datetime) -> bool:
        try:
            stamp = datetime.fromisoformat(str(metrics["last_timestamp"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
            return 0 <= age <= self.max_bar_age
        except (KeyError, TypeError, ValueError):
            return False

    def _artifact(self, session: str, now: datetime) -> dict | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00")).astimezone(
                ZoneInfo("America/New_York")
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if (
            payload.get("status") != "passed" or payload.get("kind") != "opening_research"
            or payload.get("session_date") != session or payload.get("config_hash") != self.discovery_config_hash
            or not (4 <= generated.hour < 10) or generated > now
        ):
            return None
        return payload

    def _owned_records(self, broker_state=None) -> dict[str, dict]:
        if self.store is None:
            self._last_owned_symbols = set()
            return {}
        records = {
            str(row["symbol"]).upper(): row
            for row in self.store.list_fractional_protections()
            if str(row.get("entry_intent_id", "")).startswith(f"{self.name}-")
        }
        if broker_state is not None:
            records = {symbol: row for symbol, row in records.items() if symbol in broker_state.positions}
        self._last_owned_symbols = set(records)
        return records

    def owned_position_symbols(self, broker_state=None) -> set[str]:
        return set(self._owned_records(broker_state))

    def _latest_price(self, symbol: str) -> float:
        getter = getattr(self.data, "get_latest_price", None)
        return float(getter(symbol) if getter else self.data.get_index_level(symbol))

    def _manage_owned_positions(self, broker_state, nav: float) -> TradeIntent | None:
        if broker_state is None:
            self._last_owned_symbols = set()
            return None
        for symbol, record in self._owned_records(broker_state).items():
            position = broker_state.positions[symbol]
            try:
                current_price = self._latest_price(symbol)
            except Exception:
                return self._hold(nav, f"Opening-owned {symbol} exit data unavailable; broker stop remains active", symbol)
            stop_price = float(record["stop_price"])
            take_profit = float(record["take_profit_price"])
            if current_price <= stop_price:
                return self._hold(nav, f"Opening-owned {symbol} is at its broker stop; awaiting broker execution", symbol)
            if current_price >= take_profit:
                quantity = round(abs(float(position.qty)), 4)
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=quantity,
                    order_value=current_price * quantity,
                    estimated_risk_value=0.0,
                    current_position_value=position.market_value,
                    account_nav=nav,
                    notes=f"Opening-owned take profit reached ({current_price:.2f} >= {take_profit:.2f})",
                    reference_price=current_price,
                )
        return None

    def run_cycle(self, broker_state=None) -> TradeIntent:
        nav = broker_state.account_nav if broker_state else 100000.0
        owned_exit = self._manage_owned_positions(broker_state, nav)
        if owned_exit is not None:
            return owned_exit
        now = current_time_et()
        session = now.date().isoformat()
        minute_of_day = now.hour * 60 + now.minute
        if minute_of_day < 9 * 60 + self.start_minute or minute_of_day > 9 * 60 + self.end_minute:
            return self._hold(nav, "Outside ORB/VWAP confirmation window")
        payload = self._artifact(session, now)
        if payload is None:
            return self._hold(nav, "Opening research artifact failed, stale, or unreadable")

        lanes = [("long", "buy")]
        if self.trading_lane == "stock_short_paper":
            if not self.shorts_enabled:
                return self._hold(nav, "Short paper lane is disabled by OPENING_SHORTS_ENABLED")
            lanes = [("short_research_only", "sell_short")]
        confirmed = []
        for lane, action in lanes:
            for candidate in (payload.get("lanes") or {}).get(lane) or []:
                symbol = str(candidate.get("symbol", "")).upper()
                if not symbol or (broker_state and symbol in broker_state.positions):
                    continue
                try:
                    metrics = self.data.get_opening_range(symbol, session, self.range_minutes)
                    atr = self.data.get_atr(symbol)
                except Exception:
                    continue
                last = float(metrics.get("last") or 0)
                vwap = float(metrics.get("vwap") or 0)
                volume = float(metrics.get("volume") or 0)
                if not self._fresh(metrics, now) or volume < self.min_volume:
                    continue
                if action == "buy" and last > float(metrics["high"]) and last > vwap:
                    confirmed.append(((last / float(metrics["high"])) - 1, symbol, action, last, atr, metrics))
                elif action == "sell_short" and last < float(metrics["low"]) and last < vwap:
                    confirmed.append(((float(metrics["low"]) / last) - 1, symbol, action, last, atr, metrics))
        if not confirmed:
            suffix = " (shorts disabled)" if not self.shorts_enabled else ""
            return self._hold(nav, f"No SIP candidate passed fresh ORB/VWAP confirmation{suffix}")

        _, symbol, action, price, atr, metrics = max(confirmed)
        range_risk = (price - float(metrics["low"])) if action == "buy" else (float(metrics["high"]) - price)
        risk_per_share = max(min(range_risk, atr * 1.5), price * 0.005)
        risk_budget = nav * float(os.getenv("MAX_TRADE_RISK_PCT", "0.01"))
        current_position_value = broker_state.get_position_market_value(symbol) if broker_state else 0.0
        concentration_pct = float(os.getenv("MAX_CONCENTRATION_PCT", "0.15"))
        concentration_headroom = max(0.0, nav * concentration_pct - current_position_value)
        cash = float(getattr(broker_state, "cash", nav))
        cash_buffer_pct = float(os.getenv("MIN_CASH_BUFFER_PCT", "0.10"))
        spendable_cash = max(0.0, cash - nav * cash_buffer_pct)
        max_quantity = float(os.getenv("MAX_ORDER_QUANTITY", "100"))
        raw_qty = min(
            risk_budget / risk_per_share,
            concentration_headroom / price,
            self.max_notional / price,
            spendable_cash / price,
            max_quantity,
        )
        qty = math.floor(max(0.0, raw_qty) * 10_000) / 10_000
        if qty <= 0 or price * qty < 5.0:
            return self._hold(nav, "Confirmed opening candidate cannot be sized within limits", symbol)
        direction = 1 if action == "buy" else -1
        return TradeIntent(
            strategy=self.name, symbol=symbol, action=action, quantity=qty,
            order_value=price * qty, estimated_risk_value=risk_per_share * qty,
            current_position_value=current_position_value, account_nav=nav,
            notes=f"SIP candidate confirmed by {self.range_minutes}m ORB and VWAP",
            stop_loss_price=price - direction * risk_per_share,
            take_profit_price=price + direction * risk_per_share * 2,
            reference_price=price,
        )
