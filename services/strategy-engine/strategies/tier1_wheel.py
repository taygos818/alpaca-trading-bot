import datetime
import logging
import math
import os

from .base import BaseStrategy, TradeIntent
from utils.data_feed import build_market_snapshot_provider

LOGGER = logging.getLogger(__name__)


def normal_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def calculate_delta(S: float, K: float, t: float, r: float, sigma: float, option_type: str) -> float:
    if t <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + (sigma ** 2) / 2.0) * t) / (sigma * math.sqrt(t))
    if option_type == "call":
        return normal_cdf(d1)
    else:
        return normal_cdf(d1) - 1.0


def parse_occ_symbol(occ_symbol: str, underlying: str) -> tuple[datetime.date, str, float]:
    # Format: AAPL260619P00150000 -> Expiration: 2026-06-19, Type: put, Strike: 150.0
    remaining = occ_symbol[len(underlying):]
    yymmdd = remaining[:6]
    option_type_char = remaining[6]
    strike_raw = remaining[7:]

    year = 2000 + int(yymmdd[0:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    expiration = datetime.date(year, month, day)

    option_type = "call" if option_type_char.upper() == "C" else "put"
    strike = float(strike_raw) / 1000.0
    return expiration, option_type, strike


class WheelStrategy(BaseStrategy):
    name = "tier1_wheel"

    def __init__(self):
        watchlist = os.getenv("WATCHLIST", "SPY,QQQ,MSFT,AAPL")
        self.watchlist = [item.strip() for item in watchlist.split(",") if item.strip()]
        self.data = build_market_snapshot_provider()
        self._cycle_count = 0

    def run_cycle(self, broker_state=None) -> TradeIntent:
        self._cycle_count += 1
        vix_level = self.data.get_index_level(os.getenv("VIX_SYMBOL", "VIX"))
        if vix_level > 35:
            return TradeIntent(
                strategy=self.name,
                symbol="VIX",
                action="hold",
                quantity=0,
                order_value=0.0,
                account_nav=100000.0,
                notes=f"VIX guard active at {vix_level:.2f}",
            )

        symbol = self.watchlist[self._cycle_count % len(self.watchlist)]
        nav = broker_state.account_nav if broker_state else 100000.0

        # If broker state is not available, default to sample signal logic or hold
        if not broker_state:
            iv_rank = self.data.get_iv_rank(symbol)
            if os.getenv("ENABLE_SAMPLE_SIGNALS", "").lower() == "true" and iv_rank >= 30:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell_to_open",
                    quantity=1,
                    order_value=1200.0,
                    estimated_risk_value=600.0,
                    current_position_value=5000.0,
                    account_nav=nav,
                    notes=f"Sample CSP signal with iv_rank={iv_rank:.1f}",
                )
            return TradeIntent(
                strategy=self.name,
                symbol=symbol,
                action="hold",
                quantity=0,
                order_value=0.0,
                account_nav=nav,
                notes="Awaiting broker state connectivity for options analysis",
            )

        # 1. Check existing options positions to manage / roll / take profit
        for pos_symbol, position in broker_state.positions.items():
            if pos_symbol.startswith(symbol) and len(pos_symbol) > len(symbol):
                # We have an active short option on this symbol
                qty = int(position.qty)
                if qty < 0:  # Short position
                    try:
                        expiration, option_type, strike = parse_occ_symbol(pos_symbol, symbol)
                    except Exception as exc:
                        LOGGER.warning("Could not parse OCC symbol %s: %s", pos_symbol, exc)
                        continue

                    today = datetime.date.today()
                    ts = getattr(broker_state, "timestamp", None)
                    if broker_state and ts:
                        try:
                            today = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                        except Exception:
                            pass
                    dte = (expiration - today).days

                    # Rule A: Take profit if option premium dropped by 50%
                    # Note: avg_entry_price for short option is credit received.
                    # current_price is current cost to buy back.
                    profit_take_pct = float(os.getenv("PROFIT_TAKE_PCT", "0.50"))
                    if position.avg_entry_price > 0 and position.current_price <= position.avg_entry_price * (1 - profit_take_pct):
                        return TradeIntent(
                            strategy=self.name,
                            symbol=pos_symbol,
                            action="buy_to_close",
                            quantity=abs(qty),
                            order_value=position.current_price * 100 * abs(qty),
                            estimated_risk_value=0.0,
                            current_position_value=position.market_value,
                            account_nav=nav,
                            notes=f"Profit target reached for {pos_symbol}: price={position.current_price:.2f} entry={position.avg_entry_price:.2f}",
                        )

                    # Rule B: Roll / close if DTE is 7 days or less to prevent assignment risk
                    if dte <= 7:
                        return TradeIntent(
                            strategy=self.name,
                            symbol=pos_symbol,
                            action="buy_to_close",
                            quantity=abs(qty),
                            order_value=position.current_price * 100 * abs(qty),
                            estimated_risk_value=0.0,
                            current_position_value=position.market_value,
                            account_nav=nav,
                            notes=f"Close expiring option {pos_symbol} (DTE={dte})",
                        )

                    # We already have an active option position, so hold
                    return TradeIntent(
                        strategy=self.name,
                        symbol=pos_symbol,
                        action="hold",
                        quantity=0,
                        order_value=0.0,
                        account_nav=nav,
                        notes=f"Short option position active for {pos_symbol} (DTE={dte})",
                    )

        # 2. Check stock position to determine Put vs Call side
        stock_pos = broker_state.positions.get(symbol)
        has_stock = stock_pos is not None and stock_pos.qty >= 100

        target_type = "call" if has_stock else "put"
        target_delta = 0.25 if has_stock else -0.25

        # Fetch contracts & pricing snapshots
        try:
            contracts = self.data.get_option_contracts(symbol)
            snapshots = self.data.get_option_chain_snapshots(symbol)
        except Exception as exc:
            LOGGER.error("Failed to query options data for %s: %s", symbol, exc)
            return TradeIntent(
                strategy=self.name,
                symbol=symbol,
                action="hold",
                quantity=0,
                order_value=0.0,
                account_nav=nav,
                notes=f"Options data query failed: {exc}",
            )

        underlying_price = self.data.get_index_level(symbol)

        best_contract = None
        min_delta_diff = float("inf")
        best_price = 0.0

        for contract in contracts:
            c_symbol = contract.get("symbol", "")
            c_type = contract.get("type", "").lower()
            if c_type != target_type:
                continue

            try:
                expiration, _, strike = parse_occ_symbol(c_symbol, symbol)
            except Exception:
                continue

            today = datetime.date.today()
            ts = getattr(broker_state, "timestamp", None)
            if broker_state and ts:
                try:
                    today = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                except Exception:
                    pass
            dte = (expiration - today).days
            if not (30 <= dte <= 45):
                continue

            # Check snapshot for pricing & greeks
            snap = snapshots.get(c_symbol)
            if not snap:
                continue

            quote = snap.get("latestQuote", {})
            bid = quote.get("bp") or 0.0
            ask = quote.get("ap") or 0.0
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else bid
            if mid <= 0:
                continue

            # Calculate Black-Scholes Delta (fallback if Greeks are None/missing)
            greeks = snap.get("greeks", {})
            delta = greeks.get("delta")
            if delta is None:
                # Calculate Delta locally (use 25% default implied volatility fallback)
                delta = calculate_delta(
                    S=underlying_price,
                    K=strike,
                    t=dte / 365.0,
                    r=0.045,
                    sigma=0.25,
                    option_type=c_type,
                )

            delta_diff = abs(delta - target_delta)
            if delta_diff < min_delta_diff:
                min_delta_diff = delta_diff
                best_contract = c_symbol
                best_price = mid

        if best_contract:
            try:
                _, _, best_strike = parse_occ_symbol(best_contract, symbol)
            except Exception:
                best_strike = underlying_price

            is_call = target_type == "call"
            # Quantity is 1 contract (corresponds to 100 shares of stock)
            qty = int(stock_pos.qty // 100) if is_call else 1
            if qty <= 0:
                qty = 1

            # CSP risk is strike * 100. Call risk is stock value.
            risk_val = best_strike * 100.0 * qty if not is_call else underlying_price * 100.0 * qty

            return TradeIntent(
                strategy=self.name,
                symbol=best_contract,
                action="sell_to_open",
                quantity=qty,
                order_value=best_price * 100.0 * qty,
                estimated_risk_value=risk_val,
                current_position_value=stock_pos.market_value if stock_pos else 0.0,
                account_nav=nav,
                notes=f"Sell {target_type.upper()} {best_contract} strike={best_strike} price={best_price:.2f} delta_diff={min_delta_diff:.3f}",
            )

        return TradeIntent(
            strategy=self.name,
            symbol=symbol,
            action="hold",
            quantity=0,
            order_value=0.0,
            account_nav=nav,
            notes=f"No matching {target_type} option contracts found in DTE 30-45 range",
        )
