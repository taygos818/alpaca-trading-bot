import datetime
import os
from zoneinfo import ZoneInfo

from .base import BaseStrategy, TradeIntent
from config_loader import load_strategy_config
from utils.data_feed import build_market_snapshot_provider


class IntradayStrategy(BaseStrategy):
    name = "tier3_intraday"

    def __init__(self):
        watchlist = os.getenv("WATCHLIST", "SPY,QQQ")
        self.watchlist = [item.strip() for item in watchlist.split(",") if item.strip()]
        self.data = build_market_snapshot_provider()
        self._cycle_count = 0

        # Load parameters from strategy.toml
        config_path = os.getenv("STRATEGY_CONFIG_PATH", "strategy.toml")
        parsed = load_strategy_config(config_path)
        config = parsed.get("intraday", {})
        risk_config = parsed.get("risk", {})

        self.roc_threshold = float(config.get("roc_threshold", os.getenv("INTRADAY_ROC_THRESHOLD", "0.5")))
        self.rsi_period = int(config.get("rsi_period", os.getenv("INTRADAY_RSI_PERIOD", "14")))
        self.rsi_entry_threshold = float(config.get("rsi_entry_threshold", os.getenv("INTRADAY_RSI_ENTRY_THRESHOLD", "70.0")))
        self.atr_stop_multiple = float(config.get("atr_stop_multiple", os.getenv("INTRADAY_ATR_STOP_MULTIPLE", "1.5")))
        self.risk_reward_ratio = float(config.get("risk_reward_ratio", os.getenv("INTRADAY_RISK_REWARD_RATIO", "2.0")))
        self.flatten_hour_et = int(config.get("flatten_hour_et", os.getenv("INTRADAY_FLATTEN_HOUR", "15")))
        self.flatten_minute_et = int(config.get("flatten_minute_et", os.getenv("INTRADAY_FLATTEN_MINUTE", "50")))

        # Load risk parameters
        self.max_trade_risk_pct = float(os.getenv("MAX_TRADE_RISK_PCT", config.get("max_trade_risk_pct", risk_config.get("max_trade_risk_pct", "0.01"))))
        self.max_concentration_pct = float(os.getenv("MAX_CONCENTRATION_PCT", config.get("max_concentration_pct", risk_config.get("max_concentration_pct", "0.15"))))

    def run_cycle(self, broker_state=None) -> TradeIntent:
        self._cycle_count += 1
        symbol = self.watchlist[self._cycle_count % len(self.watchlist)]
        nav = broker_state.account_nav if broker_state else 100000.0

        # Parse current time in Eastern Time (NY)
        tz = ZoneInfo("America/New_York")
        now_et = None
        if broker_state and getattr(broker_state, "timestamp", None):
            try:
                ts = broker_state.timestamp
                now_et = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
            except Exception:
                pass

        if not now_et:
            now_et = datetime.datetime.now(tz)

        # 1. Gating/Flatten Check: If we are past the flatten time, flatten any active position
        is_flatten_time = (now_et.hour > self.flatten_hour_et) or \
                          (now_et.hour == self.flatten_hour_et and now_et.minute >= self.flatten_minute_et)

        stock_pos = broker_state.positions.get(symbol) if broker_state else None

        if stock_pos and stock_pos.qty > 0:
            current_price = stock_pos.current_price
            if current_price <= 0:
                try:
                    current_price = self.data.get_index_level(symbol)
                except Exception:
                    current_price = stock_pos.avg_entry_price

            if is_flatten_time:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=int(stock_pos.qty),
                    order_value=current_price * stock_pos.qty,
                    estimated_risk_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Intraday EOD FLATTEN: {now_et.strftime('%H:%M')} >= {self.flatten_hour_et:02d}:{self.flatten_minute_et:02d}",
                )

            # Exit check: ATR trailing stop or Profit Target
            try:
                atr = self.data.get_atr(symbol)
            except Exception:
                atr = current_price * 0.01

            stop_loss = stock_pos.avg_entry_price - (self.atr_stop_multiple * atr)
            take_profit = stock_pos.avg_entry_price + (self.atr_stop_multiple * atr * self.risk_reward_ratio)

            if current_price <= stop_loss:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=int(stock_pos.qty),
                    order_value=current_price * stock_pos.qty,
                    estimated_risk_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Intraday EXIT: Stop loss hit ({current_price:.2f} <= {stop_loss:.2f})",
                )
            elif current_price >= take_profit:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="sell",
                    quantity=int(stock_pos.qty),
                    order_value=current_price * stock_pos.qty,
                    estimated_risk_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Intraday EXIT: Take profit hit ({current_price:.2f} >= {take_profit:.2f})",
                )
            else:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="hold",
                    quantity=0,
                    order_value=0.0,
                    current_position_value=stock_pos.market_value,
                    account_nav=nav,
                    notes=f"Holding intraday position. Stop={stop_loss:.2f}, Target={take_profit:.2f}, Current={current_price:.2f}",
                )

        # 2. Entry Check: Only allowed before flatten window
        if not is_flatten_time:
            try:
                roc = self.data.get_intraday_roc(symbol)
                ema_signal = self.data.get_ema_crossover(symbol)
                rsi_value = self.data.get_rsi(symbol, self.rsi_period)
                current_price = self.data.get_index_level(symbol)
                atr = self.data.get_atr(symbol)
            except Exception as exc:
                return TradeIntent(
                    strategy=self.name,
                    symbol=symbol,
                    action="hold",
                    quantity=0,
                    order_value=0.0,
                    account_nav=nav,
                    notes=f"Intraday data check failed: {exc}",
                )

            # Signal definition: ROC positive and > threshold, daily trend positive (EMA cross), not overbought
            if roc > self.roc_threshold and ema_signal and rsi_value < self.rsi_entry_threshold:
                risk_per_share = self.atr_stop_multiple * atr
                if risk_per_share <= 0:
                    risk_per_share = current_price * 0.02

                target_risk_usd = nav * self.max_trade_risk_pct
                qty = int(target_risk_usd // risk_per_share)

                # Cap by concentration
                max_order_val = nav * self.max_concentration_pct
                max_qty_by_concentration = int(max_order_val // current_price)
                qty = min(qty, max_qty_by_concentration)

                if qty > 0:
                    stop_loss_price = current_price - risk_per_share
                    take_profit_price = current_price + (risk_per_share * self.risk_reward_ratio)
                    return TradeIntent(
                        strategy=self.name,
                        symbol=symbol,
                        action="buy",
                        quantity=qty,
                        order_value=current_price * qty,
                        estimated_risk_value=risk_per_share * qty,
                        current_position_value=0.0,
                        account_nav=nav,
                        notes=f"Intraday BUY entry: ROC={roc:.2f}% > {self.roc_threshold}%, EMA cross=True, RSI={rsi_value:.1f}",
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                    )

        # Fallback to hold
        return TradeIntent(
            strategy=self.name,
            symbol=symbol,
            action="hold",
            quantity=0,
            order_value=0.0,
            account_nav=nav,
            notes="No intraday signal or in flatten window",
        )
