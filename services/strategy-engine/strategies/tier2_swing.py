import os
import math

from .base import BaseStrategy, TradeIntent
from config_loader import load_strategy_config
from utils.data_feed import build_market_snapshot_provider
from utils.market_discovery import DiscoverySettings, MarketDiscoveryError, load_current_shortlist


class SwingStrategy(BaseStrategy):
    name = "tier2_swing"

    # Default broad market liquid universe (Index ETFs + Tech/Momentum Leaders)
    CORE_MARKET_UNIVERSE = [
        "SPY", "QQQ", "IWM",
        "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO"
    ]

    def __init__(self):
        watchlist = os.getenv("WATCHLIST", "SPY,QQQ,MSFT,AAPL")
        user_watchlist = [item.strip().upper() for item in watchlist.split(",") if item.strip()]

        # Combine user watchlist with Core Market Universe
        combined = list(dict.fromkeys(user_watchlist + self.CORE_MARKET_UNIVERSE))
        self.watchlist = combined
        self.data = build_market_snapshot_provider()
        self._cycle_count = 0
        self.discovery_mode = os.getenv("DISCOVERY_MODE", "fixed").strip().lower()
        if self.discovery_mode not in {"fixed", "dynamic"}:
            raise ValueError("DISCOVERY_MODE must be fixed or dynamic")
        self.discovery_settings = DiscoverySettings.from_env()
        self.discovery_lanes = tuple(
            value.strip().lower()
            for value in os.getenv("SWING_DISCOVERY_LANES", "pullback").split(",")
            if value.strip()
        )
        if not self.discovery_lanes or any(lane not in {"pullback", "momentum", "activity"} for lane in self.discovery_lanes):
            raise ValueError("SWING_DISCOVERY_LANES contains an unsupported lane")

        # Load parameters from strategy.toml
        config_path = os.getenv("STRATEGY_CONFIG_PATH", "strategy.toml")
        parsed = load_strategy_config(config_path)
        config = parsed.get("swing", {})
        risk_config = parsed.get("risk", {})

        self.ema_fast = int(config.get("ema_fast", os.getenv("SWING_EMA_FAST", "9")))
        self.ema_slow = int(config.get("ema_slow", os.getenv("SWING_EMA_SLOW", "21")))
        self.rsi_period = int(config.get("rsi_period", os.getenv("SWING_RSI_PERIOD", "2")))
        self.rsi_entry_threshold = float(config.get("rsi_entry_threshold", os.getenv("SWING_RSI_ENTRY_THRESHOLD", "15")))
        self.atr_stop_multiple = float(config.get("atr_stop_multiple", os.getenv("SWING_ATR_STOP_MULTIPLE", "2.0")))
        self.risk_reward_ratio = float(config.get("risk_reward_ratio", os.getenv("SWING_RISK_REWARD_RATIO", "3.0")))

        # Scaling parameters (DCA dip-buying & Pyramiding)
        self.allow_scaling = str(config.get("allow_scaling", os.getenv("SWING_ALLOW_SCALING", "true"))).lower() == "true"
        self.dca_dip_pct = float(config.get("dca_dip_pct", os.getenv("SWING_DCA_DIP_PCT", "0.025")))
        self.pyramid_profit_pct = float(config.get("pyramid_profit_pct", os.getenv("SWING_PYRAMID_PROFIT_PCT", "0.020")))
        self.add_cooldown_seconds = float(config.get("add_cooldown_seconds", os.getenv("SWING_ADD_COOLDOWN_SECONDS", "300")))
        self._last_scale_timestamps: dict[str, float] = {}

        # Load risk parameters
        self.max_trade_risk_pct = float(os.getenv("MAX_TRADE_RISK_PCT", config.get("max_trade_risk_pct", risk_config.get("max_trade_risk_pct", "0.01"))))
        self.max_concentration_pct = float(os.getenv("MAX_CONCENTRATION_PCT", config.get("max_concentration_pct", risk_config.get("max_concentration_pct", "0.15"))))
        self.max_order_quantity = float(os.getenv("MAX_ORDER_QUANTITY", "100"))
        self.max_order_notional = float(os.getenv("MAX_ORDER_NOTIONAL", "10000"))
        self.cash_buffer_pct = float(os.getenv("MIN_CASH_BUFFER_PCT", "0.10"))

    def _latest_price(self, symbol: str) -> float:
        getter = getattr(self.data, "get_latest_price", None)
        return getter(symbol) if getter else self.data.get_index_level(symbol)

    def run_cycle(self, broker_state=None) -> TradeIntent:
        self._cycle_count += 1
        nav = broker_state.account_nav if broker_state else 100000.0

        if not broker_state:
            # Handle stateless / mock fallback lane signals
            symbol = self.watchlist[self._cycle_count % len(self.watchlist)]
            if os.getenv("ENABLE_SAMPLE_SIGNALS", "").lower() == "true":
                try:
                    ema_signal = self.data.get_ema_crossover(symbol, self.ema_fast, self.ema_slow)
                    rsi_value = self.data.get_rsi(symbol, self.rsi_period)
                except Exception:
                    ema_signal, rsi_value = False, 50.0

                if ema_signal and rsi_value < self.rsi_entry_threshold:
                    return TradeIntent(
                        strategy=self.name,
                        symbol=symbol,
                        action="buy",
                        quantity=10,
                        order_value=950.0,
                        estimated_risk_value=250.0,
                        current_position_value=0.0,
                        account_nav=nav,
                        notes=f"Sample swing entry with RSI={rsi_value:.1f}",
                    )
            return TradeIntent(
                strategy=self.name,
                symbol=symbol,
                action="hold",
                quantity=0,
                order_value=0.0,
                account_nav=nav,
                notes="Awaiting broker state connectivity for swing analysis",
            )

        # ---------------------------------------------------------------------
        # 1. BUILD DYNAMIC SCANNING UNIVERSE (Held Positions + Core Market)
        # ---------------------------------------------------------------------
        held_positions = {sym: pos for sym, pos in broker_state.positions.items() if pos.qty > 0}
        active_universe = list(dict.fromkeys(list(held_positions.keys()) + self.watchlist))

        # ---------------------------------------------------------------------
        # 2. PRIORITY PHASE 1: EXIT CHECKS ON ALL CURRENTLY HELD POSITIONS
        # ---------------------------------------------------------------------
        for symbol, stock_pos in held_positions.items():
            current_price = stock_pos.current_price
            if current_price <= 0:
                try:
                    current_price = self.data.get_index_level(symbol)
                except Exception:
                    current_price = stock_pos.avg_entry_price

            try:
                atr = self.data.get_atr(symbol)
            except Exception:
                atr = current_price * 0.02

            stop_loss = stock_pos.avg_entry_price - (self.atr_stop_multiple * atr)
            take_profit = stock_pos.avg_entry_price + (self.atr_stop_multiple * atr * self.risk_reward_ratio)

            if current_price <= stop_loss:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=round(stock_pos.qty, 4),
                    order_value=current_price * stock_pos.qty,
                    estimated_risk_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Swing EXIT: Stop loss hit ({current_price:.2f} <= {stop_loss:.2f})",
                )
            elif current_price >= take_profit:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=round(stock_pos.qty, 4),
                    order_value=current_price * stock_pos.qty,
                    estimated_risk_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Swing EXIT: Take profit hit ({current_price:.2f} >= {take_profit:.2f})",
                )

        if self.discovery_mode == "dynamic":
            try:
                discovery = load_current_shortlist(
                    self.discovery_settings.output_path,
                    expected_config_hash=self.discovery_settings.config_hash(),
                )
                discovered_symbols = []
                for lane in self.discovery_lanes:
                    discovered_symbols.extend(item["symbol"] for item in discovery["lanes"].get(lane, []))
                if not discovered_symbols:
                    raise MarketDiscoveryError("Configured discovery lanes contain no candidates")
                active_universe = list(dict.fromkeys(list(held_positions.keys()) + discovered_symbols))
            except MarketDiscoveryError as exc:
                return TradeIntent(
                    strategy=self.name,
                    symbol=next(iter(held_positions), "DISCOVERY"),
                    action="hold",
                    quantity=0,
                    order_value=0.0,
                    account_nav=nav,
                    notes=f"Dynamic discovery unavailable; entries blocked: {exc}",
                )
        # ---------------------------------------------------------------------
        # 3. BROAD MARKET OPPORTUNITY SCANNING & SCALING (Rank Top Setups)
        # ---------------------------------------------------------------------
        valid_candidates = []

        for symbol in active_universe:
            existing_pos = held_positions.get(symbol)
            current_pos_val = existing_pos.market_value if existing_pos else 0.0
            max_allowed_pos_val = nav * self.max_concentration_pct
            available_headroom = max_allowed_pos_val - current_pos_val

            # Minimum available headroom to consider a new buy order ($5 minimum order size)
            if available_headroom < 5.0:
                continue

            try:
                ema_signal = self.data.get_ema_crossover(symbol, self.ema_fast, self.ema_slow)
                rsi_value = self.data.get_rsi(symbol, self.rsi_period)
                current_price = self._latest_price(symbol)
                atr = self.data.get_atr(symbol)
            except Exception:
                continue

            if existing_pos and existing_pos.qty > 0:
                # Evaluate held position for controlled scaling (DCA or Pyramiding)
                if not self.allow_scaling:
                    continue

                import time
                now_ts = time.time()
                last_add_ts = self._last_scale_timestamps.get(symbol, 0.0)
                if now_ts - last_add_ts < self.add_cooldown_seconds:
                    continue

                avg_entry = existing_pos.avg_entry_price
                if avg_entry <= 0:
                    continue

                pnl_pct = (current_price - avg_entry) / avg_entry

                # Rule 1: DCA Dip-Buy (Price dropped <= -dca_dip_pct + setup)
                is_dca = (pnl_pct <= -self.dca_dip_pct) and ema_signal and (rsi_value < self.rsi_entry_threshold)

                # Rule 2: Pyramiding Momentum Add (Price in profit >= pyramid_profit_pct + setup)
                is_pyramid = (pnl_pct >= self.pyramid_profit_pct) and ema_signal and (rsi_value < self.rsi_entry_threshold)

                if is_dca or is_pyramid:
                    scale_type = "DCA Dip-Buy" if is_dca else "Pyramid Momentum Add"
                    valid_candidates.append({
                        "symbol": symbol,
                        "rsi": rsi_value,
                        "price": current_price,
                        "atr": atr,
                        "available_headroom": available_headroom,
                        "current_pos_val": current_pos_val,
                        "is_scale": True,
                        "scale_type": scale_type,
                        "pnl_pct": pnl_pct,
                    })
            else:
                if ema_signal and rsi_value < self.rsi_entry_threshold:
                    valid_candidates.append({
                        "symbol": symbol,
                        "rsi": rsi_value,
                        "price": current_price,
                        "atr": atr,
                        "available_headroom": available_headroom,
                        "current_pos_val": current_pos_val,
                        "is_scale": False,
                    })

        # If valid market entries exist, pick the BEST candidate (lowest RSI oversold score)
        if valid_candidates:
            valid_candidates.sort(key=lambda c: c["rsi"])
            best = valid_candidates[0]

            symbol = best["symbol"]
            current_price = best["price"]
            atr = best["atr"]
            rsi_value = best["rsi"]
            available_headroom = best["available_headroom"]
            current_pos_val = best["current_pos_val"]
            is_scale = best.get("is_scale", False)

            risk_per_share = self.atr_stop_multiple * atr
            if risk_per_share <= 0:
                risk_per_share = current_price * 0.05

            target_risk_usd = nav * self.max_trade_risk_pct
            risk_quantity = target_risk_usd / risk_per_share
            concentration_quantity = available_headroom / current_price
            notional_quantity = self.max_order_notional / current_price
            cash = float(getattr(broker_state, "cash", nav))
            spendable_cash = max(0.0, cash - (nav * self.cash_buffer_pct))
            cash_quantity = spendable_cash / current_price
            raw_quantity = min(
                risk_quantity,
                concentration_quantity,
                notional_quantity,
                cash_quantity,
                self.max_order_quantity,
            )
            qty = math.floor(max(0.0, raw_quantity) * 10_000) / 10_000

            stop_loss_price = current_price - risk_per_share
            take_profit_price = current_price + (risk_per_share * self.risk_reward_ratio)

            if is_scale:
                import time
                self._last_scale_timestamps[symbol] = time.time()
                note_text = f"Swing {best['scale_type']} [{symbol}]: PnL={best['pnl_pct']*100:.1f}%, EMA cross=True, RSI={rsi_value:.1f}"
            else:
                note_text = f"Market Scan #1 Opportunity [{symbol}]: EMA cross=True, RSI={rsi_value:.1f} (Ranked top out of {len(valid_candidates)} candidates)"

            order_value = current_price * qty
            if qty > 0 and order_value >= 5.0:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="buy",
                    quantity=qty,
                    order_value=order_value,
                    estimated_risk_value=risk_per_share * qty,
                    current_position_value=current_pos_val,
                    account_nav=nav,
                    notes=note_text,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                )


        # ---------------------------------------------------------------------
        # 4. DEFAULT ROTATION LOGGING / HOLDING
        # ---------------------------------------------------------------------
        if held_positions:
            held_symbol = next(iter(held_positions.keys()))
            return TradeIntent(
                strategy=self.name,
                symbol=held_symbol,
                action="hold",
                quantity=0,
                order_value=0.0,
                account_nav=nav,
                notes=f"Holding swing position in {held_symbol}.",
            )

        active_symbol = active_universe[self._cycle_count % len(active_universe)]
        return TradeIntent(
            strategy=self.name,
            symbol=active_symbol,
            action="hold",
            quantity=0,
            order_value=0.0,
            account_nav=nav,
            notes=f"Market scan complete across {len(active_universe)} tickers. Scanned {active_symbol}: no entry signal.",
        )
