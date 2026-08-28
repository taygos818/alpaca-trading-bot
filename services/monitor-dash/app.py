import json
import os
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import redis
import requests
import pyotp
import robin_stocks.robinhood as rh
from flask import Flask, jsonify, Response, render_template



app = Flask(__name__)
TRADE_LOG_PATH = Path(os.getenv("TRADE_LOG_PATH", "/app/logs/trades.jsonl"))
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TRADE_COUNTER = Counter()
LAST_SUMMARY_KEY = ""

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_PAPER_TRADE = os.getenv("ALPACA_PAPER_TRADE", "true").strip().lower() == "true"

ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME", "").strip()
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD", "").strip()
ROBINHOOD_TOTP_SECRET = os.getenv("ROBINHOOD_TOTP_SECRET", "").strip()
ACTIVE_BROKER = os.getenv("ACTIVE_BROKER", "alpaca").strip().lower()



def load_recent_trades(limit: int = 50):
    if POSTGRES_DSN:
        try:
            with psycopg.connect(POSTGRES_DSN) as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute(
                        """
                        SELECT timestamp, strategy, symbol, action, quantity,
                               order_value, estimated_risk_value, allowed, reason
                        FROM trades
                        WHERE state_source IN ('alpaca', 'lane_policy')
                        ORDER BY timestamp DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    return cur.fetchall()
        except Exception:
            pass

    if not TRADE_LOG_PATH.exists():
        return []
    entries = []
    with TRADE_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return list(reversed(entries[-limit:]))


def summarize_pnl():
    trades = load_recent_trades(limit=500)
    total_notional = sum(float(item.get("order_value", 0.0)) for item in trades if item.get("allowed"))
    allowed_count = sum(1 for item in trades if item.get("allowed"))
    blocked_count = sum(1 for item in trades if not item.get("allowed"))
    return {
        "trade_events": len(trades),
        "allowed_events": allowed_count,
        "blocked_events": blocked_count,
        "gross_notional_usd": round(total_notional, 2),
    }


def post_daily_summary_forever():
    global LAST_SUMMARY_KEY
    timezone = ZoneInfo("America/New_York")
    summary_hour = int(os.getenv("DAILY_SUMMARY_HOUR_ET", "16"))
    summary_minute = int(os.getenv("DAILY_SUMMARY_MINUTE_ET", "30"))

    while True:
        now = datetime.now(timezone)
        summary_key = now.strftime("%Y-%m-%d")
        should_post = (
            DISCORD_WEBHOOK_URL
            and now.weekday() < 5
            and now.hour == summary_hour
            and now.minute >= summary_minute
            and LAST_SUMMARY_KEY != summary_key
        )
        if should_post:
            payload = summarize_pnl()
            message = (
                f"Daily summary {summary_key}: "
                f"events={payload['trade_events']} allowed={payload['allowed_events']} "
                f"blocked={payload['blocked_events']} gross_notional=${payload['gross_notional_usd']}"
            )
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10).raise_for_status()
            LAST_SUMMARY_KEY = summary_key
        time.sleep(60)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/paper")
def paper_dashboard():
    return render_template("paper.html")


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "trade_log_exists": TRADE_LOG_PATH.exists(),
            "postgres_configured": bool(POSTGRES_DSN),
        }
    )


@app.get("/trades")
def trades():
    recent = load_recent_trades()
    for trade in recent:
        TRADE_COUNTER["trades_total"] += 1
    return jsonify(recent)


@app.get("/pnl")
def pnl():
    return jsonify(summarize_pnl())


def get_alpaca_headers(paper: bool = False):
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret
    }


def get_alpaca_url(path: str, paper: bool = False):
    return f"https://paper-api.alpaca.markets{path}"


def fetch_alpaca_account(paper: bool = True):
    headers = get_alpaca_headers(paper=paper)
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return None
    try:
        r = requests.get(get_alpaca_url("/v2/account", paper=paper), headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            data["paper_trade"] = paper
            return data
        else:
            app.logger.warning("Alpaca account fetch HTTP %s (paper=%s): %s", r.status_code, paper, r.text)
    except Exception as e:
        app.logger.error("Failed to fetch Alpaca account (paper=%s): %s", paper, e)
    return None


def fetch_alpaca_positions(paper: bool = False):
    headers = get_alpaca_headers(paper=paper)
    if not headers["APCA-API-KEY-ID"] or not headers["APCA-API-SECRET-KEY"]:
        return []
    try:
        r = requests.get(get_alpaca_url("/v2/positions", paper=paper), headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        app.logger.error("Failed to fetch Alpaca positions (paper=%s): %s", paper, e)
    return []


def fetch_robinhood_info():
    if not ROBINHOOD_USERNAME or not ROBINHOOD_PASSWORD:
        return None
    try:
        if ROBINHOOD_TOTP_SECRET:
            totp = pyotp.TOTP(ROBINHOOD_TOTP_SECRET)
            totp_code = totp.now()
            rh.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD, store_session=False, mfa_code=totp_code)
        else:
            rh.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD, store_session=False)

        portfolio_profile = rh.profiles.load_portfolio_profile()
        account_profile = rh.profiles.load_account_profile()
        holdings = rh.account.build_holdings()

        rh.logout()
        return {
            "portfolio": portfolio_profile,
            "account": account_profile,
            "holdings": holdings
        }
    except Exception as e:
        app.logger.error("Failed to fetch Robinhood info: %s", e)
    return None


@app.get("/alpaca_info")
def alpaca_info():
    from flask import request
    is_paper_requested = request.args.get("paper", "").lower() == "true"
    
    account = fetch_alpaca_account(paper=is_paper_requested)
    positions = fetch_alpaca_positions(paper=is_paper_requested)

    if not account:
        if not is_paper_requested:
            return jsonify({
                "status": "broker_data_unavailable",
                "message": "This observation-only dashboard is not configured with brokerage credentials."
            }), 200
        return jsonify({"status": "error", "message": "Could not connect to Alpaca Paper API"}), 500
    
    total_unrealized_pnl = 0.0
    for pos in positions:
        try:
            total_unrealized_pnl += float(pos.get("unrealized_pnl", 0.0))
        except (TypeError, ValueError):
            pass

    return jsonify({
        "status": "ok",
        "account": {
            "equity": float(account.get("equity", 0.0)),
            "cash": float(account.get("cash", 0.0)),
            "buying_power": float(account.get("buying_power", 0.0)),
            "portfolio_value": float(account.get("portfolio_value", 0.0)),
            "account_number": account.get("account_number"),
            "paper_trade": account.get("paper_trade", False),
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        },
        "positions": [
            {
                "symbol": pos.get("symbol"),
                "qty": float(pos.get("qty", 0.0)),
                "market_value": float(pos.get("market_value", 0.0)),
                "avg_entry_price": float(pos.get("avg_entry_price", 0.0)),
                "current_price": float(pos.get("current_price", 0.0)),
                "unrealized_intraday_pnl": float(pos.get("unrealized_intraday_pnl", 0.0)),
                "unrealized_pnl": float(pos.get("unrealized_pnl", 0.0)),
                "unrealized_pnl_pct": float(pos.get("unrealized_pnl_pct", 0.0)) * 100.0,
            }
            for pos in positions
        ]
    })


def compute_fifo_trades(trades_list):
    symbol_queues = {}
    matched_trades = []
    sorted_trades = sorted(trades_list, key=lambda t: t["timestamp"])

    for t in sorted_trades:
        symbol = t["symbol"]
        action = t["action"].lower()
        qty = int(t["quantity"])
        if qty <= 0:
            continue
        val = float(t["order_value"])
        price = val / qty if qty > 0 else 0.0
        timestamp = t["timestamp"].isoformat() if hasattr(t["timestamp"], "isoformat") else str(t["timestamp"])
        strategy = t["strategy"]

        if symbol not in symbol_queues:
            symbol_queues[symbol] = deque()

        q = symbol_queues[symbol]
        is_buy = action in {"buy", "buy_to_open", "buy_to_close"}
        is_sell = action in {"sell", "sell_to_open", "sell_to_close"}

        if not is_buy and not is_sell:
            continue

        if len(q) == 0:
            q.append({
                "timestamp": timestamp,
                "quantity": qty,
                "price": price,
                "is_buy": is_buy,
                "strategy": strategy
            })
        else:
            front = q[0]
            if front["is_buy"] != is_buy:
                remaining_qty_to_match = qty
                while remaining_qty_to_match > 0 and len(q) > 0 and q[0]["is_buy"] != is_buy:
                    match_item = q[0]
                    match_qty = min(remaining_qty_to_match, match_item["quantity"])

                    if match_item["is_buy"]:
                        chunk_pnl = match_qty * (price - match_item["price"])
                        direction = "Long"
                    else:
                        chunk_pnl = match_qty * (match_item["price"] - price)
                        direction = "Short"

                    matched_trades.append({
                        "symbol": symbol,
                        "direction": direction,
                        "quantity": match_qty,
                        "entry_time": match_item["timestamp"],
                        "exit_time": timestamp,
                        "entry_price": round(match_item["price"], 2),
                        "exit_price": round(price, 2),
                        "realized_pnl": round(chunk_pnl, 2),
                        "strategy": strategy,
                    })

                    remaining_qty_to_match -= match_qty
                    match_item["quantity"] -= match_qty
                    if match_item["quantity"] <= 0:
                        q.popleft()

                if remaining_qty_to_match > 0:
                    q.append({
                        "timestamp": timestamp,
                        "quantity": remaining_qty_to_match,
                        "price": price,
                        "is_buy": is_buy,
                        "strategy": strategy
                    })
            else:
                q.append({
                    "timestamp": timestamp,
                    "quantity": qty,
                    "price": price,
                    "is_buy": is_buy,
                    "strategy": strategy
                })

    return matched_trades


@app.get("/live_performance")
def live_performance():
    if not POSTGRES_DSN:
        return jsonify({
            "status": "no_postgres",
            "metrics": {
                "total_realized_pnl": 0.0,
                "win_rate_pct": 0.0,
                "trades_closed": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "profit_factor": 0.0
            },
            "matched_trades": []
        })

    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT t.timestamp, t.strategy, t.symbol, t.action, t.quantity, t.order_value, t.estimated_risk_value
                    FROM trades t
                    LEFT JOIN order_events oe ON t.intent_id = oe.intent_id
                    WHERE t.allowed = true
                      AND t.state_source = 'alpaca'
                      AND (oe.status IS NULL OR oe.status != 'executor_error')
                    ORDER BY t.timestamp ASC
                    """
                )
                trades_list = cur.fetchall()
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    matched = compute_fifo_trades(trades_list)

    trades_closed = len(matched)
    winning_trades = sum(1 for t in matched if t["realized_pnl"] > 0)
    losing_trades = sum(1 for t in matched if t["realized_pnl"] < 0)
    win_rate_pct = round((winning_trades / trades_closed * 100.0), 2) if trades_closed > 0 else 0.0
    total_realized_pnl = round(sum(t["realized_pnl"] for t in matched), 2)

    gross_profits = sum(t["realized_pnl"] for t in matched if t["realized_pnl"] > 0)
    gross_losses = abs(sum(t["realized_pnl"] for t in matched if t["realized_pnl"] < 0))
    profit_factor = round(gross_profits / gross_losses, 2) if gross_losses > 0 else (round(gross_profits, 2) if gross_profits > 0 else 1.0)

    matched_reversed = list(reversed(matched))

    return jsonify({
        "status": "ok",
        "metrics": {
            "total_realized_pnl": total_realized_pnl,
            "win_rate_pct": win_rate_pct,
            "trades_closed": trades_closed,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "profit_factor": profit_factor
        },
        "matched_trades": matched_reversed
    })


@app.get("/live_equity")
def live_equity():
    if not POSTGRES_DSN:
        return jsonify({"status": "no_postgres", "snapshots": []})

    try:
        with psycopg.connect(POSTGRES_DSN) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS account_snapshots (
                        id BIGSERIAL PRIMARY KEY,
                        timestamp TIMESTAMPTZ NOT NULL,
                        account_nav DOUBLE PRECISION NOT NULL,
                        cash DOUBLE PRECISION NOT NULL,
                        long_market_value DOUBLE PRECISION NOT NULL,
                        short_market_value DOUBLE PRECISION NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    SELECT timestamp, account_nav, cash, long_market_value, short_market_value
                    FROM account_snapshots
                    ORDER BY timestamp ASC
                    LIMIT 2000
                    """
                )
                snapshots = cur.fetchall()

                formatted_snapshots = []
                for s in snapshots:
                    ts = s["timestamp"]
                    formatted_snapshots.append({
                        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                        "account_nav": s["account_nav"],
                        "cash": s["cash"],
                        "long_market_value": s["long_market_value"],
                        "short_market_value": s["short_market_value"]
                    })
                return jsonify({
                    "status": "ok",
                    "snapshots": formatted_snapshots
                })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.get("/backtest")
def backtest():
    backtest_file = TRADE_LOG_PATH.parent / "last_backtest.json"
    if not backtest_file.exists():
        return jsonify({"status": "no_data"})
    try:
        with open(backtest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.get("/api/service/status")
def service_status():
    is_paused = False
    is_market_open = True
    if REDIS_URL:
        try:
            r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
            paused_val = r.get("bot_paused")
            if paused_val == "true":
                is_paused = True
            market_val = r.get("market_open")
            if market_val == "false":
                is_market_open = False
        except Exception:
            pass
    return jsonify({
        "status": "PAUSED" if is_paused else "RUNNING",
        "bot_paused": is_paused,
        "market_open": is_market_open,
        "lane": os.getenv("TRADING_LANE", "stock_paper"),
        "strategy": os.getenv("BOT_STRATEGY", "tier2_swing"),
    })


@app.get("/api/logs")
def get_logs():
    limit = 100
    logs = []
    if TRADE_LOG_PATH.exists():
        try:
            with TRADE_LOG_PATH.open("r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
                for line in reversed(lines[-limit:]):
                    try:
                        logs.append(json.loads(line))
                    except Exception:
                        logs.append({"raw": line})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500
    return jsonify({"status": "ok", "count": len(logs), "logs": logs})


@app.get("/api/quote/<symbol>")
def get_symbol_quote(symbol: str):
    symbol = symbol.upper().strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400

    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            headers = {
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            }
            data_url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"
            resp = requests.get(data_url, headers=headers, timeout=5)
            if resp.ok:
                trade_data = resp.json().get("trade", {})
                price = float(trade_data.get("p", 0.0))
                if price > 0:
                    return jsonify({
                        "status": "ok",
                        "symbol": symbol,
                        "price": price,
                        "timestamp": trade_data.get("t"),
                        "source": "alpaca"
                    })
        except Exception as e:
            app.logger.warning("Alpaca data fetch failed for %s: %s", symbol, e)

    fallback_prices = {"TSLA": 171.86, "SPY": 585.50, "QQQ": 510.20, "AAPL": 225.40, "MSFT": 448.90, "NVDA": 130.50}
    price = fallback_prices.get(symbol, 150.00)
    return jsonify({
        "status": "ok",
        "symbol": symbol,
        "price": price,
        "change_pct": 0.12,
        "source": "fallback"
    })


REDIS_URL = os.getenv("REDIS_URL", "")


def get_aggregate_trade_metrics():
    metrics_dict = {
        "gross_notional_usd": 0.0,
        "trades_allowed_total": 0,
        "trades_blocked_total": 0
    }
    if POSTGRES_DSN:
        try:
            with psycopg.connect(POSTGRES_DSN, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            COALESCE(SUM(CASE WHEN t.allowed = true AND t.state_source = 'alpaca' AND (oe.status IS NULL OR oe.status != 'executor_error') THEN t.order_value ELSE 0 END), 0),
                            COUNT(CASE WHEN t.allowed = true AND t.state_source = 'alpaca' AND (oe.status IS NULL OR oe.status != 'executor_error') THEN 1 END),
                            COUNT(CASE WHEN t.allowed = false THEN 1 END)
                        FROM trades t
                        LEFT JOIN order_events oe ON t.intent_id = oe.intent_id
                        """
                    )
                    row = cur.fetchone()
                    if row:
                        metrics_dict["gross_notional_usd"] = float(row[0])
                        metrics_dict["trades_allowed_total"] = int(row[1])
                        metrics_dict["trades_blocked_total"] = int(row[2])
                        return metrics_dict
        except Exception as e:
            app.logger.warning("Failed to query DB for metrics: %s", e)

    # Fallback to local logs
    if TRADE_LOG_PATH.exists():
        try:
            total_notional = 0.0
            allowed_count = 0
            blocked_count = 0
            with TRADE_LOG_PATH.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        allowed = item.get("allowed", False)
                        if allowed:
                            allowed_count += 1
                            total_notional += float(item.get("order_value", 0.0))
                        else:
                            blocked_count += 1
                    except Exception:
                        pass
            metrics_dict["gross_notional_usd"] = round(total_notional, 2)
            metrics_dict["trades_allowed_total"] = allowed_count
            metrics_dict["trades_blocked_total"] = blocked_count
        except Exception as e:
            app.logger.warning("Failed to read trade logs for metrics fallback: %s", e)

    return metrics_dict


def get_redis_metrics():
    metrics_dict = {
        "engine_cycle_duration_seconds": 0.0,
        "engine_last_heartbeat_timestamp": 0,
        "engine_cycles_total": 0
    }
    if not REDIS_URL:
        return metrics_dict
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
        duration = r.get("metrics:engine_cycle_duration_seconds")
        heartbeat = r.get("metrics:engine_last_heartbeat")
        cycles = r.get("metrics:engine_cycles_total")
        
        if duration is not None:
            metrics_dict["engine_cycle_duration_seconds"] = float(duration)
        if heartbeat is not None:
            metrics_dict["engine_last_heartbeat_timestamp"] = int(heartbeat)
        if cycles is not None:
            metrics_dict["engine_cycles_total"] = int(cycles)
    except Exception as e:
        app.logger.warning("Failed to query Redis for metrics: %s", e)
    return metrics_dict


@app.get("/metrics")
def metrics():
    db_stats = get_aggregate_trade_metrics()
    redis_stats = get_redis_metrics()
    
    body = "\n".join(
        [
            "# HELP trades_total Total trade events served by the dashboard",
            "# TYPE trades_total counter",
            f"trades_total {TRADE_COUNTER['trades_total']}",
            
            "# HELP gross_notional_usd Gross notional across allowed events",
            "# TYPE gross_notional_usd gauge",
            f"gross_notional_usd {db_stats['gross_notional_usd']}",
            
            "# HELP trades_allowed_total Total allowed trades",
            "# TYPE trades_allowed_total counter",
            f"trades_allowed_total {db_stats['trades_allowed_total']}",
            
            "# HELP trades_blocked_total Total blocked trades",
            "# TYPE trades_blocked_total counter",
            f"trades_blocked_total {db_stats['trades_blocked_total']}",
            
            "# HELP engine_cycle_duration_seconds Last engine cycle duration in seconds",
            "# TYPE engine_cycle_duration_seconds gauge",
            f"engine_cycle_duration_seconds {redis_stats['engine_cycle_duration_seconds']}",
            
            "# HELP engine_last_heartbeat_timestamp UNIX timestamp of the last engine cycle heartbeat",
            "# TYPE engine_last_heartbeat_timestamp gauge",
            f"engine_last_heartbeat_timestamp {redis_stats['engine_last_heartbeat_timestamp']}",
            
            "# HELP engine_cycles_total Total strategy execution loops run by engine",
            "# TYPE engine_cycles_total counter",
            f"engine_cycles_total {redis_stats['engine_cycles_total']}",
        ]
    )
    return Response(body + "\n", mimetype="text/plain; version=0.0.4")


def start_background_threads():
    thread = threading.Thread(target=post_daily_summary_forever, daemon=True)
    thread.start()


if __name__ == "__main__":
    start_background_threads()
    app.run(host="0.0.0.0", port=8090)
