import datetime
import math
import os
import re
from pathlib import Path
import json

from strategies.base import TradeIntent
from utils.broker_state import BrokerState, BrokerPosition

# Black-Scholes mathematical functions
def normal_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_bs_price(S: float, K: float, t: float, r: float, sigma: float, option_type: str) -> float:
    if t <= 0:
        if option_type == "call":
            return max(0.0, S - K)
        else:
            return max(0.0, K - S)
    if sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
        
    d1 = (math.log(S / K) + (r + (sigma ** 2) / 2.0) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    
    if option_type == "call":
        return S * normal_cdf(d1) - K * math.exp(-r * t) * normal_cdf(d2)
    else:
        return K * math.exp(-r * t) * normal_cdf(-d2) - S * normal_cdf(-d1)

def calculate_bs_delta(S: float, K: float, t: float, r: float, sigma: float, option_type: str) -> float:
    if t <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        else:
            return -1.0 if S < K else 0.0
    if sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
        
    d1 = (math.log(S / K) + (r + (sigma ** 2) / 2.0) * t) / (sigma * math.sqrt(t))
    if option_type == "call":
        return normal_cdf(d1)
    else:
        return normal_cdf(d1) - 1.0

def parse_occ_symbol(occ_symbol: str, underlying: str) -> tuple[datetime.date, str, float]:
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

def generate_occ_symbol(underlying: str, expiration: datetime.date, option_type: str, strike: float) -> str:
    yymmdd = expiration.strftime("%y%m%d")
    type_char = "C" if option_type == "call" else "P"
    strike_raw = f"{int(strike * 1000.0):08d}"
    return f"{underlying.upper()}{yymmdd}{type_char}{strike_raw}"

# Helper for calculation indicators on sliced historical data
def calculate_ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    ema = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema = ((price - ema) * multiplier) + ema
    return ema

def calculate_rsi(closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for prev, curr in zip(closes[-period-1:], closes[-period:]):
        change = curr - prev
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr(bars: list[dict], period: int) -> float:
    if len(bars) < period + 1:
        return 1.0
    tr_list = []
    for i in range(len(bars) - period - 1, len(bars)):
        if i <= 0:
            continue
        high = float(bars[i].get("h", bars[i].get("c")))
        low = float(bars[i].get("l", bars[i].get("c")))
        prev_close = float(bars[i-1].get("c"))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list) if tr_list else 1.0


class HistoricalSnapshotProvider:
    def __init__(self, history_data: dict[str, list[dict]], current_date: datetime.date, vix_data: list[dict] | None = None):
        self.history_data = history_data
        self.current_date = current_date
        
        # Sliced historical data up to current_date
        self.sliced_data = {}
        for symbol, bars in history_data.items():
            self.sliced_data[symbol] = [
                bar for bar in bars 
                if datetime.datetime.strptime(bar["t"].split("T")[0], "%Y-%m-%d").date() <= current_date
            ]
            
        self.vix_level = 18.0
        if vix_data:
            matching_vix = [
                obs for obs in vix_data 
                if datetime.datetime.strptime(obs["date"], "%Y-%m-%d").date() <= current_date
            ]
            if matching_vix:
                try:
                    val = matching_vix[-1].get("value", "18.0")
                    self.vix_level = float(val) if val != "." else 18.0
                except ValueError:
                    pass

    def get_index_level(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol == "VIX":
            return self.vix_level
        bars = self.sliced_data.get(symbol)
        if not bars:
            raise RuntimeError(f"No historical data for {symbol} on/before {self.current_date}")
        return float(bars[-1]["c"])

    def get_iv_rank(self, symbol: str) -> float:
        return self.vix_level  # Simple mock IV rank mapped to VIX index

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        symbol = symbol.upper()
        bars = self.sliced_data.get(symbol)
        if not bars:
            return False
        closes = [float(bar["c"]) for bar in bars]
        fast = fast_period or 9
        slow = slow_period or 21
        return calculate_ema(closes, fast) > calculate_ema(closes, slow)

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        symbol = symbol.upper()
        bars = self.sliced_data.get(symbol)
        if not bars:
            return 50.0
        closes = [float(bar["c"]) for bar in bars]
        p = period or 14
        return calculate_rsi(closes, p)

    def get_intraday_roc(self, symbol: str) -> float:
        symbol = symbol.upper()
        bars = self.sliced_data.get(symbol)
        if not bars:
            return 0.0
        last_bar = bars[-1]
        open_price = float(last_bar.get("o", last_bar.get("c")))
        close_price = float(last_bar.get("c"))
        if open_price == 0:
            return 0.0
        return ((close_price - open_price) / open_price) * 100.0


    def get_atr(self, symbol: str, period: int = 14) -> float:
        symbol = symbol.upper()
        bars = self.sliced_data.get(symbol)
        if not bars:
            return 1.0
        return calculate_atr(bars, period)

    def get_option_contracts(self, symbol: str) -> list[dict]:
        # Generate hypothetical option contracts expiring in 30-45 days around the current stock price
        symbol = symbol.upper()
        underlying_price = self.get_index_level(symbol)
        
        # Target expiration 35 days in the future
        exp_date = self.current_date + datetime.timedelta(days=35)
        # Standard OCC style date
        exp_str = exp_date.strftime("%Y-%m-%d")
        
        contracts = []
        # Generate strikes around underlying: +/- 10%
        strike_pcts = [-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10]
        for pct in strike_pcts:
            strike = round(underlying_price * (1 + pct))
            if strike <= 0:
                continue
            for otype in ["call", "put"]:
                occ_symbol = generate_occ_symbol(symbol, exp_date, otype, strike)
                contracts.append({
                    "symbol": occ_symbol,
                    "strike_price": str(strike),
                    "expiration_date": exp_str,
                    "type": otype
                })
        return contracts

    def get_option_chain_snapshots(self, symbol: str) -> dict[str, dict]:
        symbol = symbol.upper()
        contracts = self.get_option_contracts(symbol)
        underlying_price = self.get_index_level(symbol)
        
        # Compute BS option prices
        snapshots = {}
        for contract in contracts:
            c_symbol = contract["symbol"]
            strike = float(contract["strike_price"])
            otype = contract["type"]
            
            exp_date, _, _ = parse_occ_symbol(c_symbol, symbol)
            dte = (exp_date - self.current_date).days
            t = dte / 365.0
            
            # Estimate IV from VIX
            iv = self.vix_level / 100.0
            r = 0.045
            
            price = calculate_bs_price(underlying_price, strike, t, r, iv, otype)
            delta = calculate_bs_delta(underlying_price, strike, t, r, iv, otype)
            
            snapshots[c_symbol] = {
                "latestQuote": {
                    "bp": max(0.01, round(price - 0.05, 2)),
                    "ap": max(0.02, round(price + 0.05, 2))
                },
                "latestTrade": {"price": round(price, 2)},
                "greeks": {"delta": delta}
            }
        return snapshots


class BacktestSimulator:
    def __init__(self, initial_equity: float = 100000.0):
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.positions: dict[str, BrokerPosition] = {}
        self.daily_history = []
        self.trades = []
        
    def run(self, strategy_class, watchlist: list[str], start_date: datetime.date, end_date: datetime.date):
        # Load historical files
        project_root = Path(__file__).resolve().parents[1]
        historical_dir = project_root / "data" / "historical"
        
        history_data = {}
        for symbol in watchlist:
            file_path = historical_dir / f"{symbol.upper()}_daily.json"
            if not file_path.exists():
                raise FileNotFoundError(f"Historical file not found: {file_path.name}. Please run download_history.py first.")
            with open(file_path, "r", encoding="utf-8") as f:
                history_data[symbol.upper()] = json.load(f)

        vix_data = None
        vix_file = historical_dir / "VIX_daily.json"
        if vix_file.exists():
            with open(vix_file, "r", encoding="utf-8") as f:
                vix_data = json.load(f)

        # Collect and sort all available dates
        all_dates_set = set()
        for symbol, bars in history_data.items():
            for bar in bars:
                date_str = bar["t"].split("T")[0]
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                if start_date <= dt <= end_date:
                    all_dates_set.add(dt)
        all_dates = sorted(list(all_dates_set))

        if not all_dates:
            print("No business days found in the selected date range.")
            return

        # Initialize strategy instance
        # Mock os.environ to set WATCHLIST and STRATEGY_CONFIG_PATH
        prev_watchlist = os.environ.get("WATCHLIST")
        os.environ["WATCHLIST"] = ",".join(watchlist)
        
        try:
            strategy = strategy_class()
        finally:
            if prev_watchlist:
                os.environ["WATCHLIST"] = prev_watchlist
            else:
                os.environ.pop("WATCHLIST", None)

        # Load risk parameters from strategy.toml
        import tomllib
        config_path = os.getenv("STRATEGY_CONFIG_PATH", "strategy.toml")
        max_trade_risk_pct = 0.01
        max_concentration_pct = 0.15
        if os.path.exists(config_path):
            try:
                with open(config_path, "rb") as f:
                    parsed = tomllib.load(f)
                    risk_config = parsed.get("risk", {})
                    max_trade_risk_pct = float(risk_config.get("max_trade_risk_pct", 0.01))
                    max_concentration_pct = float(risk_config.get("max_concentration_pct", 0.15))
            except Exception:
                pass

        # Env overrides (takes precedence for backtesting sweeps)
        if os.getenv("MAX_TRADE_RISK_PCT"):
            max_trade_risk_pct = float(os.getenv("MAX_TRADE_RISK_PCT"))
        if os.getenv("MAX_CONCENTRATION_PCT"):
            max_concentration_pct = float(os.getenv("MAX_CONCENTRATION_PCT"))

        print(f"Running backtest for {strategy.name} from {all_dates[0]} to {all_dates[-1]}...")

        # Main time-loop simulation
        for current_date in all_dates:
            # 1. Update prices for all held positions based on today's close
            data_provider = HistoricalSnapshotProvider(history_data, current_date, vix_data)
            
            # Resolve expirations and option prices
            expired_options = []
            for pos_symbol, pos in list(self.positions.items()):
                # Check if it is an option (long OCC symbol name)
                if len(pos_symbol) > 8 and any(c in pos_symbol for c in ("C", "P")):
                    underlying = None
                    for w in watchlist:
                        if pos_symbol.startswith(w.upper()):
                            underlying = w.upper()
                            break
                    if not underlying:
                        continue
                        
                    exp_date, otype, strike = parse_occ_symbol(pos_symbol, underlying)
                    
                    # If expiration date is reached or passed
                    if current_date >= exp_date:
                        # Expiration resolution
                        underlying_close = data_provider.get_index_level(underlying)
                        qty = abs(pos.qty) # Short option is negative quantity
                        
                        if otype == "put":
                            # Assigned! We buy stock at strike
                            if underlying_close <= strike:
                                assignment_cost = strike * 100.0 * qty
                                self.cash -= assignment_cost
                                # Add stock position
                                stock_pos = self.positions.get(underlying)
                                current_stock_qty = stock_pos.qty if stock_pos else 0.0
                                new_stock_qty = current_stock_qty + (100.0 * qty)
                                self.positions[underlying] = BrokerPosition(
                                    symbol=underlying,
                                    qty=new_stock_qty,
                                    market_value=new_stock_qty * underlying_close,
                                    avg_entry_price=strike,
                                    current_price=underlying_close
                                )
                                self.trades.append({
                                    "date": str(current_date),
                                    "symbol": underlying,
                                    "action": "buy",
                                    "qty": int(100 * qty),
                                    "price": strike,
                                    "notes": f"Put Assigned from {pos_symbol}"
                                })
                        else:  # call option
                            # Assigned! We sell stock at strike
                            if underlying_close >= strike:
                                stock_pos = self.positions.get(underlying)
                                if stock_pos and stock_pos.qty >= 100 * qty:
                                    self.cash += strike * 100.0 * qty
                                    new_stock_qty = stock_pos.qty - (100.0 * qty)
                                    if new_stock_qty <= 0:
                                        self.positions.pop(underlying, None)
                                    else:
                                        self.positions[underlying] = BrokerPosition(
                                            symbol=underlying,
                                            qty=new_stock_qty,
                                            market_value=new_stock_qty * underlying_close,
                                            avg_entry_price=stock_pos.avg_entry_price,
                                            current_price=underlying_close
                                        )
                                    self.trades.append({
                                        "date": str(current_date),
                                        "symbol": underlying,
                                        "action": "sell",
                                        "qty": int(100 * qty),
                                        "price": strike,
                                        "notes": f"Call Assigned from {pos_symbol}"
                                    })
                        expired_options.append(pos_symbol)
                    else:
                        # Price the option dynamically using Black-Scholes
                        underlying_price = data_provider.get_index_level(underlying)
                        t = (exp_date - current_date).days / 365.0
                        iv = data_provider.vix_level / 100.0
                        oprice = calculate_bs_price(underlying_price, strike, t, 0.045, iv, otype)
                        
                        # Market value of short option is negative
                        self.positions[pos_symbol] = BrokerPosition(
                            symbol=pos_symbol,
                            qty=pos.qty,
                            market_value=pos.qty * oprice * 100.0,
                            avg_entry_price=pos.avg_entry_price,
                            current_price=oprice
                        )
                else:
                    # Stock position: update current close price
                    close_price = data_provider.get_index_level(pos_symbol)
                    self.positions[pos_symbol] = BrokerPosition(
                        symbol=pos_symbol,
                        qty=pos.qty,
                        market_value=pos.qty * close_price,
                        avg_entry_price=pos.avg_entry_price,
                        current_price=close_price
                    )
            
            for o in expired_options:
                self.positions.pop(o, None)

            # 2. Re-calculate equity
            stock_val = sum(pos.market_value for pos_symbol, pos in self.positions.items())
            total_equity = self.cash + stock_val

            # 3. Construct broker state for strategy run
            broker_state = BrokerState(
                timestamp=datetime.datetime.combine(current_date, datetime.time(15, 0, 0)).isoformat() + "Z",
                account_id="backtest-sim",
                account_nav=total_equity,
                buying_power=self.cash,
                trading_blocked=False,
                positions=self.positions.copy()
            )

            # Set strategy data feed
            strategy.data = data_provider

            # 4. Run cycle
            intent = strategy.run_cycle(broker_state)

            # 5. Process Intent / Execute Trade
            if intent and intent.action in ("buy", "sell", "sell_to_open", "sell_to_close", "buy_to_open", "buy_to_close"):
                # Sizing checks / Risk manager simulation
                # Calculate estimated risk value
                risk_val = intent.estimated_risk_value if intent.estimated_risk_value is not None else intent.order_value
                max_risk = total_equity * max_trade_risk_pct
                
                # Exclude exits from size limits
                is_exit = intent.action in ("sell", "sell_to_close", "buy_to_close")
                
                # Check concentration
                current_pos_val = self.positions.get(intent.symbol).market_value if self.positions.get(intent.symbol) else 0.0
                post_trade_exposure = current_pos_val + intent.order_value
                concentration_limit = total_equity * max_concentration_pct
                
                allowed = True
                reason = "Approved"
                if not is_exit:
                    if risk_val > max_risk:
                        allowed = False
                        reason = f"Trade risk {risk_val:.2f} exceeds limit {max_risk:.2f}"
                    elif post_trade_exposure > concentration_limit:
                        allowed = False
                        reason = f"Concentration limit exceeded: {post_trade_exposure:.2f} > {concentration_limit:.2f}"
                
                if allowed:
                    self._execute_trade(intent, current_date, data_provider)
                else:
                    pass  # Blocked by Risk Manager

            # 5.5 Intraday Strategy EOD Flatten and Stop/Target Simulation
            if strategy.name == "tier3_intraday":
                for pos_symbol, pos in list(self.positions.items()):
                    if pos.qty > 0:
                        bars = data_provider.sliced_data.get(pos_symbol)
                        last_bar = bars[-1] if bars else {}
                        open_p = float(last_bar.get("o", pos.avg_entry_price))
                        high_p = float(last_bar.get("h", pos.avg_entry_price))
                        low_p = float(last_bar.get("l", pos.avg_entry_price))
                        close_p = float(last_bar.get("c", pos.avg_entry_price))

                        try:
                            atr = data_provider.get_atr(pos_symbol)
                        except Exception:
                            atr = open_p * 0.01

                        stop_loss = pos.avg_entry_price - (strategy.atr_stop_multiple * atr)
                        take_profit = pos.avg_entry_price + (strategy.atr_stop_multiple * atr * strategy.risk_reward_ratio)

                        exit_price = close_p
                        exit_notes = "Intraday EOD Flatten"

                        if low_p <= stop_loss and high_p >= take_profit:
                            exit_price = stop_loss
                            exit_notes = f"Intraday Exit: Stop loss hit ({low_p:.2f} <= {stop_loss:.2f})"
                        elif low_p <= stop_loss:
                            exit_price = stop_loss
                            exit_notes = f"Intraday Exit: Stop loss hit ({low_p:.2f} <= {stop_loss:.2f})"
                        elif high_p >= take_profit:
                            exit_price = take_profit
                            exit_notes = f"Intraday Exit: Take profit hit ({high_p:.2f} >= {take_profit:.2f})"

                        self.cash += exit_price * pos.qty
                        self.trades.append({
                            "date": str(current_date),
                            "symbol": pos_symbol,
                            "action": "sell",
                            "qty": int(pos.qty),
                            "price": exit_price,
                            "notes": exit_notes
                        })
                        self.positions.pop(pos_symbol, None)

            # Recompute end-of-day stats
            stock_val = sum(pos.market_value for pos_symbol, pos in self.positions.items())
            total_equity = self.cash + stock_val
            self.daily_history.append({
                "date": current_date,
                "equity": total_equity,
                "cash": self.cash,
                "positions_value": stock_val
            })

        print("Backtest simulation completed successfully.")

    def _execute_trade(self, intent: TradeIntent, current_date: datetime.date, data_provider: HistoricalSnapshotProvider):
        # We fill orders at the daily close price
        qty = intent.quantity
        symbol = intent.symbol.upper()
        
        if intent.action == "buy":
            if intent.strategy == "tier3_intraday":
                bars = data_provider.sliced_data.get(symbol)
                last_bar = bars[-1] if bars else {}
                entry_price = float(last_bar.get("o", last_bar.get("c")))
            else:
                entry_price = data_provider.get_index_level(symbol)
            cost = entry_price * qty
            if self.cash >= cost:
                self.cash -= cost
                pos = self.positions.get(symbol)
                old_qty = pos.qty if pos else 0.0
                old_entry = pos.avg_entry_price if pos else 0.0
                new_qty = old_qty + qty
                new_entry = ((old_qty * old_entry) + cost) / new_qty
                
                self.positions[symbol] = BrokerPosition(
                    symbol=symbol,
                    qty=new_qty,
                    market_value=new_qty * entry_price,
                    avg_entry_price=new_entry,
                    current_price=entry_price
                )
                self.trades.append({
                    "date": str(current_date),
                    "symbol": symbol,
                    "action": "buy",
                    "qty": qty,
                    "price": entry_price,
                    "notes": intent.notes
                })
                
        elif intent.action == "sell":
            close_price = data_provider.get_index_level(symbol)
            pos = self.positions.get(symbol)
            if pos and pos.qty >= qty:
                self.cash += close_price * qty
                new_qty = pos.qty - qty
                if new_qty <= 0:
                    self.positions.pop(symbol, None)
                else:
                    self.positions[symbol] = BrokerPosition(
                        symbol=symbol,
                        qty=new_qty,
                        market_value=new_qty * close_price,
                        avg_entry_price=pos.avg_entry_price,
                        current_price=close_price
                    )
                self.trades.append({
                    "date": str(current_date),
                    "symbol": symbol,
                    "action": "sell",
                    "qty": qty,
                    "price": close_price,
                    "notes": intent.notes
                })
                
        elif intent.action == "sell_to_open":
            # Short option: cash increase (credit)
            # Find price of this option Symbol (like SPY260717P00250000)
            underlying = None
            for w in data_provider.history_data.keys():
                if symbol.startswith(w):
                    underlying = w
                    break
            if not underlying:
                return
                
            snapshots = data_provider.get_option_chain_snapshots(underlying)
            snap = snapshots.get(symbol)
            if not snap:
                return
            mid_price = snap["latestTrade"]["price"]
            
            credit = mid_price * 100.0 * qty
            self.cash += credit
            
            # Short position gets negative quantity
            pos = self.positions.get(symbol)
            old_qty = pos.qty if pos else 0.0
            new_qty = old_qty - qty
            
            self.positions[symbol] = BrokerPosition(
                symbol=symbol,
                qty=new_qty,
                market_value=new_qty * mid_price * 100.0,
                avg_entry_price=mid_price,
                current_price=mid_price
            )
            self.trades.append({
                "date": str(current_date),
                "symbol": symbol,
                "action": "sell_to_open",
                "qty": qty,
                "price": mid_price,
                "notes": intent.notes
            })
            
        elif intent.action == "buy_to_close":
            # Buy back option: cash decrease (debit)
            underlying = None
            for w in data_provider.history_data.keys():
                if symbol.startswith(w):
                    underlying = w
                    break
            if not underlying:
                return
                
            snapshots = data_provider.get_option_chain_snapshots(underlying)
            snap = snapshots.get(symbol)
            if not snap:
                return
            mid_price = snap["latestTrade"]["price"]
            
            cost = mid_price * 100.0 * qty
            self.cash -= cost
            
            pos = self.positions.get(symbol)
            if pos:
                new_qty = pos.qty + qty  # short is negative, so adding qty closes it
                if new_qty >= 0:
                    self.positions.pop(symbol, None)
                else:
                    self.positions[symbol] = BrokerPosition(
                        symbol=symbol,
                        qty=new_qty,
                        market_value=new_qty * mid_price * 100.0,
                        avg_entry_price=pos.avg_entry_price,
                        current_price=mid_price
                    )
                self.trades.append({
                    "date": str(current_date),
                    "symbol": symbol,
                    "action": "buy_to_close",
                    "qty": qty,
                    "price": mid_price,
                    "notes": intent.notes
                })

    def get_metrics(self) -> dict:
        if not self.daily_history:
            return {}
            
        ending_equity = self.daily_history[-1]["equity"]
        total_return = (ending_equity - self.initial_equity) / self.initial_equity
        
        # Calculate drawdown
        peak = self.initial_equity
        max_dd = 0.0
        for entry in self.daily_history:
            eq = entry["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
                
        # Win rate
        wins = 0
        losses = 0
        for t in self.trades:
            # We count trades. For simplicity, let's track execution notes.
            # Real performance win-rate would require FIFO matching, but we can compute approximate count of trade events.
            pass
            
        # Sharpe ratio
        daily_returns = []
        for prev, curr in zip(self.daily_history[:-1], self.daily_history[1:]):
            daily_returns.append((curr["equity"] - prev["equity"]) / prev["equity"])
            
        mean_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
        variance = sum((x - mean_ret) ** 2 for x in daily_returns) / len(daily_returns) if len(daily_returns) > 1 else 0.0
        std_dev = math.sqrt(variance)
        
        # Annualized Sharpe (assuming 252 business days, risk-free rate = 0%)
        sharpe = (mean_ret / std_dev) * math.sqrt(252) if std_dev > 0 else 0.0

        return {
            "initial_equity": self.initial_equity,
            "ending_equity": ending_equity,
            "total_return_pct": total_return * 100.0,
            "max_drawdown_pct": max_dd * 100.0,
            "sharpe_ratio": sharpe,
            "total_trades": len(self.trades)
        }
