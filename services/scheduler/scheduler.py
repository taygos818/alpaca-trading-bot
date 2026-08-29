import datetime
import logging
import os
import signal
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
import psycopg
import redis
import requests
from utils.alpaca_credentials import alpaca_credentials

from heartbeat import HeartbeatWriter
from notifications import DiscordNotifier, EmailNotifier


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("scheduler")
SHOULD_RUN = True

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
redis_client = None
ALPACA_CONSECUTIVE_FAILURES = 0
ALPACA_OUTAGE_ALERTED = False
ENGINE_HEARTBEAT_ALERTED = False


def check_engine_heartbeat():
    global ENGINE_HEARTBEAT_ALERTED
    path = Path(os.getenv("ENGINE_HEARTBEAT_PATH", "/app/paper-data/engine-heartbeat"))
    maximum_age = float(os.getenv("ENGINE_HEARTBEAT_MAX_AGE_SECONDS", "90"))
    age = time.time() - path.stat().st_mtime if path.exists() else float("inf")
    healthy = 0 <= age <= maximum_age
    control = get_redis_client()
    if control:
        control.mset(
            {
                "strategy_engine_heartbeat_status": "ok" if healthy else "stale",
                "strategy_engine_heartbeat_checked_at": str(time.time()),
                "strategy_engine_heartbeat_age_seconds": str(age),
            }
        )
    if healthy and ENGINE_HEARTBEAT_ALERTED:
        ENGINE_HEARTBEAT_ALERTED = False
        message = "Contest strategy-engine heartbeat recovered."
        DiscordNotifier.from_env().send(f"[Trading Bot Recovery] {message}")
        EmailNotifier.from_env().send("[Recovery] Contest engine heartbeat", message)
    elif not healthy and not ENGINE_HEARTBEAT_ALERTED:
        ENGINE_HEARTBEAT_ALERTED = True
        message = f"Contest strategy-engine heartbeat is stale or missing (age={age:.0f}s)."
        DiscordNotifier.from_env().send(f"[Trading Bot Critical] {message}")
        EmailNotifier.from_env().send("[Critical] Contest engine heartbeat stale", message)
    LOGGER.info("Strategy-engine heartbeat healthy=%s age_seconds=%s", healthy, round(age, 1))
    return healthy


def record_alpaca_connectivity(verified: bool, failure_type: str = ""):
    global ALPACA_CONSECUTIVE_FAILURES, ALPACA_OUTAGE_ALERTED
    threshold = int(os.getenv("ALPACA_CONNECTIVITY_ALERT_THRESHOLD", "3"))
    if verified:
        was_alerted = ALPACA_OUTAGE_ALERTED
        ALPACA_CONSECUTIVE_FAILURES = 0
        ALPACA_OUTAGE_ALERTED = False
    else:
        ALPACA_CONSECUTIVE_FAILURES += 1
        was_alerted = False

    r = get_redis_client()
    if r:
        try:
            r.mset({
                "alpaca_connectivity_status": "ok" if verified else "degraded",
                "alpaca_connectivity_updated_at": str(time.time()),
                "alpaca_connectivity_failures": str(ALPACA_CONSECUTIVE_FAILURES),
            })
        except Exception as exc:
            LOGGER.error("Failed to publish Alpaca connectivity state: %s", type(exc).__name__)

    discord = DiscordNotifier.from_env()
    email = EmailNotifier.from_env()
    try:
        if verified and was_alerted:
            message = "Alpaca API connectivity recovered; market controls remain fail-closed until verified."
            discord.send(f"[Trading Bot Recovery] {message}")
            email.send("[Recovery] Alpaca API connectivity restored", message)
        elif not verified and ALPACA_CONSECUTIVE_FAILURES >= threshold and not ALPACA_OUTAGE_ALERTED:
            ALPACA_OUTAGE_ALERTED = True
            message = (
                f"Alpaca API connectivity failed {ALPACA_CONSECUTIVE_FAILURES} consecutive checks "
                f"({failure_type or 'unknown'}); trading is fail-closed."
            )
            discord.send(f"[Trading Bot Critical] {message}")
            email.send("[Critical] Alpaca API connectivity failure", message)
    except Exception as exc:
        LOGGER.error("Failed to deliver Alpaca connectivity alert: %s", type(exc).__name__)


def get_redis_client():
    global redis_client
    if redis_client is None:
        try:
            redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            # Ping once to check connection
            redis_client.ping()
        except Exception as e:
            LOGGER.error(f"Failed to connect to Redis at {REDIS_URL}: {e}")
            redis_client = None
    return redis_client


def get_alpaca_base_url() -> str:
    is_paper = os.getenv("ALPACA_PAPER_TRADE", "true").lower() == "true"
    if not is_paper:
        raise ValueError("alpaca-trading-bot is paper-only")
    base_url = os.getenv("ALPACA_TRADING_API_URL", "https://paper-api.alpaca.markets").rstrip("/")
    if base_url != "https://paper-api.alpaca.markets":
        raise ValueError("alpaca-trading-bot accepts only the Alpaca paper endpoint")
    return base_url


def check_market_hours():
    api_key, secret_key = alpaca_credentials(os.getenv("ALPACA_PAPER_TRADE", "true").lower() == "true")
    base_url = get_alpaca_base_url()

    is_open = False
    clock_verified = False

    failure_type = ""
    if not api_key or not secret_key:
        failure_type = "credentials_missing"
        LOGGER.error("Alpaca API credentials not found. Failing market_open closed.")
    else:
        url = f"{base_url}/v2/clock"
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "accept": "application/json",
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload.get("is_open"), bool):
                    is_open = payload["is_open"]
                    clock_verified = True
                LOGGER.info(f"Successfully fetched market clock. is_open: {is_open}")
            else:
                failure_type = f"http_{response.status_code}"
                LOGGER.warning(
                    f"Alpaca Clock API returned status code {response.status_code}. "
                    "Failing market_open closed."
                )
        except Exception as e:
            failure_type = type(e).__name__
            LOGGER.warning(f"Error querying Alpaca Clock API: {type(e).__name__}. Failing market_open closed.")

    record_alpaca_connectivity(clock_verified, failure_type)

    r = get_redis_client()
    if r:
        try:
            pipeline = r.pipeline()
            pipeline.set("market_open", "true" if is_open and clock_verified else "false")
            pipeline.set("market_open_updated_at", str(time.time()))
            pipeline.setnx("bot_paused", "true")
            pipeline.execute()
            LOGGER.info("Wrote verified market_open = %s to Redis", is_open and clock_verified)
        except Exception as e:
            LOGGER.error(f"Failed to write to Redis: {e}")
    else:
        LOGGER.error("Redis client is not initialized. Cannot write market_open status.")


def take_account_snapshot():
    api_key, secret_key = alpaca_credentials(os.getenv("ALPACA_PAPER_TRADE", "true").lower() == "true")
    base_url = get_alpaca_base_url()
    postgres_dsn = os.getenv("POSTGRES_DSN")

    if not api_key or not secret_key or not postgres_dsn:
        LOGGER.warning("Missing API keys or POSTGRES_DSN. Skipping account snapshot.")
        return

    url = f"{base_url}/v2/account"

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            payload = response.json()
            equity = float(payload.get("equity") or payload.get("portfolio_value") or 0.0)
            cash = float(payload.get("cash") or 0.0)
            long_val = float(payload.get("long_market_value") or 0.0)
            short_val = float(payload.get("short_market_value") or 0.0)

            with psycopg.connect(postgres_dsn) as conn:
                with conn.cursor() as cur:
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
                        INSERT INTO account_snapshots (timestamp, account_nav, cash, long_market_value, short_market_value)
                        VALUES (NOW(), %s, %s, %s, %s)
                        """,
                        (equity, cash, long_val, short_val),
                    )
            LOGGER.info("Recorded account snapshot: nav=%s, cash=%s", equity, cash)
        else:
            LOGGER.warning(
                f"Alpaca Account API returned status code {response.status_code} "
                f"during snapshot: {response.text}"
            )
    except Exception as e:
        LOGGER.error(f"Error taking account snapshot: {e}")


def send_daily_performance_report():
    api_key, secret_key = alpaca_credentials(os.getenv("ALPACA_PAPER_TRADE", "true").lower() == "true")
    base_url = get_alpaca_base_url()
    postgres_dsn = os.getenv("POSTGRES_DSN")

    if not api_key or not secret_key or not postgres_dsn:
        LOGGER.warning("Missing API keys or POSTGRES_DSN. Skipping daily performance report.")
        return

    LOGGER.info("Starting compilation of daily performance report...")

    # 1. Fetch current account info
    account_url = f"{base_url}/v2/account"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "accept": "application/json",
    }

    try:
        acct_resp = requests.get(account_url, headers=headers, timeout=10)
        if acct_resp.status_code != 200:
            LOGGER.error(f"Alpaca Account API error: {acct_resp.status_code} - {acct_resp.text}")
            return
        acct_data = acct_resp.json()
        
        current_nav = float(acct_data.get("equity") or acct_data.get("portfolio_value") or 0.0)
        cash = float(acct_data.get("cash") or 0.0)
        long_val = float(acct_data.get("long_market_value") or 0.0)
        short_val = float(acct_data.get("short_market_value") or 0.0)
    except Exception as e:
        LOGGER.error(f"Error fetching account info for daily report: {e}")
        return

    # 2. Fetch current positions
    positions = []
    try:
        pos_resp = requests.get(f"{base_url}/v2/positions", headers=headers, timeout=10)
        if pos_resp.status_code == 200:
            positions = pos_resp.json()
        else:
            LOGGER.warning(f"Alpaca Positions API returned {pos_resp.status_code}: {pos_resp.text}")
    except Exception as e:
        LOGGER.error(f"Error fetching positions for daily report: {e}")

    # 3. Fetch previous NAV and executed trades from PostgreSQL
    prev_nav = None
    recent_trades = []
    try:
        with psycopg.connect(postgres_dsn) as conn:
            with conn.cursor() as cur:
                # 3a. Get snapshot closest to 24 hours ago
                cur.execute(
                    """
                    SELECT account_nav FROM account_snapshots 
                    WHERE timestamp >= NOW() - INTERVAL '24 hours' 
                    ORDER BY timestamp ASC LIMIT 1;
                    """
                )
                row = cur.fetchone()
                if row:
                    prev_nav = float(row[0])
                else:
                    # Fallback: oldest snapshot in the DB
                    cur.execute(
                        """
                        SELECT account_nav FROM account_snapshots 
                        ORDER BY timestamp ASC LIMIT 1;
                        """
                    )
                    row = cur.fetchone()
                    if row:
                        prev_nav = float(row[0])

                # 3b. Get recent order events in last 24h
                cur.execute(
                    """
                    SELECT timestamp, strategy, symbol, action, quantity, status, broker_order_id, error_message
                    FROM order_events
                    WHERE timestamp >= NOW() - INTERVAL '24 hours'
                    ORDER BY timestamp DESC;
                    """
                )
                recent_trades = cur.fetchall()
    except Exception as e:
        LOGGER.error(f"Error fetching DB data for daily report: {e}")

    # 4. Calculate performance metrics
    if prev_nav is None or prev_nav == 0.0:
        prev_nav = current_nav
    
    nav_change = current_nav - prev_nav
    nav_change_pct = (nav_change / prev_nav * 100.0) if prev_nav > 0 else 0.0

    # 5. Format HTML Email Content
    notifier = EmailNotifier.from_env()

    subject = f"[Daily Performance Update] NAV: ${current_nav:,.2f} ({'+' if nav_change >= 0 else ''}{nav_change_pct:.2f}%)"

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #0f172a;
            color: #e2e8f0;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #1e293b;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            border: 1px solid #334155;
        }}
        .header {{
            background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%);
            padding: 24px;
            text-align: center;
            border-bottom: 1px solid #334155;
        }}
        .header h1 {{
            margin: 0;
            font-size: 20px;
            color: #ffffff;
            font-weight: 600;
            letter-spacing: 0.5px;
        }}
        .content {{
            padding: 24px;
        }}
        .stats-grid {{
            display: table;
            width: 100%;
            margin-bottom: 24px;
        }}
        .stats-row {{
            display: table-row;
        }}
        .stat-card-cell {{
            display: table-cell;
            width: 50%;
            padding: 8px;
        }}
        .stat-card {{
            background-color: #0f172a;
            padding: 16px;
            border-radius: 8px;
            border: 1px solid #334155;
        }}
        .stat-label {{
            font-size: 11px;
            text-transform: uppercase;
            color: #94a3b8;
            font-weight: 600;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }}
        .stat-value {{
            font-size: 18px;
            font-weight: 700;
            color: #f8fafc;
        }}
        .stat-value.positive {{
            color: #10b981;
        }}
        .stat-value.negative {{
            color: #ef4444;
        }}
        .section-title {{
            font-size: 14px;
            font-weight: 600;
            color: #3b82f6;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin: 24px 0 12px 0;
            border-bottom: 1px solid #334155;
            padding-bottom: 6px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 16px;
            font-size: 12px;
            text-align: left;
        }}
        th {{
            background-color: #0f172a;
            color: #94a3b8;
            font-weight: 600;
            padding: 8px 12px;
            border-bottom: 1px solid #334155;
        }}
        td {{
            padding: 8px 12px;
            border-bottom: 1px solid #334155;
            color: #cbd5e1;
        }}
        .positive {{ color: #10b981; }}
        .negative {{ color: #ef4444; }}
        .badge {{
            display: inline-block;
            padding: 2px 6px;
            font-size: 10px;
            font-weight: 600;
            border-radius: 4px;
            text-transform: uppercase;
        }}
        .badge-buy {{ background-color: rgba(16, 185, 129, 0.2); color: #10b981; }}
        .badge-sell {{ background-color: rgba(239, 68, 68, 0.2); color: #ef4444; }}
        .footer {{
            background-color: #0f172a;
            padding: 16px;
            text-align: center;
            font-size: 10px;
            color: #64748b;
            border-top: 1px solid #334155;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>DAILY PERFORMANCE SUMMARY</h1>
        </div>
        <div class="content">
            <div class="stats-grid">
                <div class="stats-row">
                    <div class="stat-card-cell" style="width: 100%; display: block;">
                        <div class="stat-card">
                            <div class="stat-label">Net Asset Value (NAV)</div>
                            <div class="stat-value" style="font-size: 24px;">${current_nav:,.2f}</div>
                        </div>
                    </div>
                </div>
                <div class="stats-row">
                    <div class="stat-card-cell">
                        <div class="stat-card">
                            <div class="stat-label">24h Change</div>
                            <div class="stat-value {'positive' if nav_change >= 0 else 'negative'}">
                                {'+' if nav_change >= 0 else ''}${nav_change:,.2f} ({'+' if nav_change >= 0 else ''}{nav_change_pct:.2f}%)
                            </div>
                        </div>
                    </div>
                    <div class="stat-card-cell">
                        <div class="stat-card">
                            <div class="stat-label">Cash Balance</div>
                            <div class="stat-value">${cash:,.2f}</div>
                        </div>
                    </div>
                </div>
                <div class="stats-row">
                    <div class="stat-card-cell">
                        <div class="stat-card">
                            <div class="stat-label">Long Market Value</div>
                            <div class="stat-value">${long_val:,.2f}</div>
                        </div>
                    </div>
                    <div class="stat-card-cell">
                        <div class="stat-card">
                            <div class="stat-label">Short Market Value</div>
                            <div class="stat-value">${short_val:,.2f}</div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="section-title">Active Positions</div>"""

    if not positions:
        html_content += "\n            <p style='font-size: 12px; color: #94a3b8; margin: 0 0 16px 0;'>No active positions.</p>"
    else:
        html_content += """
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Qty</th>
                        <th>Avg Entry</th>
                        <th>Current Price</th>
                        <th>Market Value</th>
                        <th>Unrealized P&L</th>
                    </tr>
                </thead>
                <tbody>"""
        for pos in positions:
            symbol = pos.get("symbol")
            qty = float(pos.get("qty", 0.0))
            avg_entry = float(pos.get("avg_entry_price", 0.0))
            current_price = float(pos.get("current_price", 0.0))
            mkt_val = float(pos.get("market_value", 0.0))
            unrealized_pl = float(pos.get("unrealized_pl", 0.0))
            unrealized_plpc = float(pos.get("unrealized_plpc", 0.0)) * 100.0

            pl_class = "positive" if unrealized_pl >= 0 else "negative"
            pl_sign = "+" if unrealized_pl >= 0 else ""

            html_content += f"""
                    <tr>
                        <td><strong>{symbol}</strong></td>
                        <td>{qty:g}</td>
                        <td>${avg_entry:,.2f}</td>
                        <td>${current_price:,.2f}</td>
                        <td>${mkt_val:,.2f}</td>
                        <td class="{pl_class}">{pl_sign}${unrealized_pl:,.2f} ({pl_sign}{unrealized_plpc:.2f}%)</td>
                    </tr>"""
        html_content += "\n                </tbody>\n            </table>"

    html_content += "\n            <div class='section-title'>Executed Trades (Last 24 Hours)</div>"

    if not recent_trades:
        html_content += "\n            <p style='font-size: 12px; color: #94a3b8; margin: 0;'>No trades executed in the last 24 hours.</p>"
    else:
        html_content += """
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Strategy</th>
                        <th>Symbol</th>
                        <th>Action</th>
                        <th>Qty</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>"""
        for t in recent_trades:
            t_time = t[0]
            time_str = t_time.strftime("%H:%M:%S") if hasattr(t_time, "strftime") else str(t_time)[:19]
            strat = t[1]
            sym = t[2]
            action = t[3].upper()
            qty = t[4]
            status = t[5]
            
            badge_class = "badge-buy" if "BUY" in action else "badge-sell"

            html_content += f"""
                    <tr>
                        <td>{time_str}</td>
                        <td>{strat}</td>
                        <td><strong>{sym}</strong></td>
                        <td><span class="badge {badge_class}">{action}</span></td>
                        <td>{qty}</td>
                        <td>{status}</td>
                    </tr>"""
        html_content += "\n                </tbody>\n            </table>"

    utc_now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    html_content += f"""
        </div>
        <div class="footer">
            Trading Bot Daily Performance Reporter • Generated at {utc_now_str}
        </div>
    </div>
</body>
</html>
"""

    # Format Plain Text Email Content
    plain_content = f"""DAILY PERFORMANCE SUMMARY
=========================
Net Asset Value (NAV): ${current_nav:,.2f}
24h Change: {'+' if nav_change >= 0 else ''}${nav_change:,.2f} ({'+' if nav_change >= 0 else ''}{nav_change_pct:.2f}%)
Cash Balance: ${cash:,.2f}
Long Market Value: ${long_val:,.2f}
Short Market Value: ${short_val:,.2f}

ACTIVE POSITIONS
================
"""
    if not positions:
        plain_content += "No active positions.\n"
    else:
        for pos in positions:
            symbol = pos.get("symbol")
            qty = float(pos.get("qty", 0.0))
            avg_entry = float(pos.get("avg_entry_price", 0.0))
            current_price = float(pos.get("current_price", 0.0))
            mkt_val = float(pos.get("market_value", 0.0))
            unrealized_pl = float(pos.get("unrealized_pl", 0.0))
            unrealized_plpc = float(pos.get("unrealized_plpc", 0.0)) * 100.0
            pl_sign = "+" if unrealized_pl >= 0 else ""
            plain_content += f"{symbol}: Qty={qty:g}, AvgEntry=${avg_entry:,.2f}, Current=${current_price:,.2f}, Value=${mkt_val:,.2f}, P&L={pl_sign}${unrealized_pl:,.2f} ({pl_sign}{unrealized_plpc:.2f}%)\n"

    plain_content += "\nEXECUTED TRADES (LAST 24 HOURS)\n===============================\n"
    if not recent_trades:
        plain_content += "No trades executed in the last 24 hours.\n"
    else:
        for t in recent_trades:
            t_time = t[0]
            time_str = t_time.strftime("%H:%M:%S") if hasattr(t_time, "strftime") else str(t_time)[:19]
            plain_content += f"[{time_str}] {t[1]} - {t[2]} {t[3].upper()} Qty={t[4]} Status={t[5]}\n"

    plain_content += f"\nGenerated at {utc_now_str}\n"

    # Send report
    notifier.send(subject=subject, body=plain_content, html_body=html_content)


def cleanup_database():
    postgres_dsn = os.getenv("POSTGRES_DSN")
    if not postgres_dsn:
        return
    try:
        with psycopg.connect(postgres_dsn) as conn:
            with conn.cursor() as cur:
                # Retain high-frequency account_snapshots for 90 days, purge older items
                cur.execute(
                    """
                    DELETE FROM account_snapshots
                    WHERE timestamp < NOW() - INTERVAL '90 days';
                    """
                )
        LOGGER.info("Database maintenance completed: pruned account_snapshots older than 90 days.")
    except Exception as e:
        LOGGER.error(f"Error during DB maintenance: {e}")


def run_market_discovery():
    from utils.market_discovery import (
        AlpacaMarketDiscovery,
        DiscoverySettings,
        MarketDiscoveryError,
        load_current_shortlist,
        write_discovery_failure,
    )

    settings = DiscoverySettings.from_env()
    try:
        control = get_redis_client()
        if control is None or control.get("market_open") != "true":
            LOGGER.info("Market discovery skipped because verified market_open is not true")
            return
        try:
            current = load_current_shortlist(
                settings.output_path,
                expected_config_hash=settings.config_hash(),
            )
            LOGGER.info(
                "Market discovery skipped because a passing current-session artifact already exists: config=%s shortlist=%s",
                current["config_hash"],
                len(current["symbols"]),
            )
            return
        except MarketDiscoveryError:
            pass
        result = AlpacaMarketDiscovery(settings).run()
        LOGGER.info(
            "Market discovery completed: universe=%s qualified=%s shortlist=%s config=%s",
            result["universe_count"], result["qualified_count"], len(result["symbols"]), result["config_hash"],
        )
    except Exception as exc:
        write_discovery_failure(settings, exc)
        if type(exc).__name__ == "MarketDiscoveryError":
            LOGGER.error("Market discovery failed closed: %s: %s", type(exc).__name__, exc)
        else:
            LOGGER.error("Market discovery failed closed: %s", type(exc).__name__)


def run_premarket_discovery():
    from utils.market_discovery import AlpacaMarketDiscovery, DiscoverySettings, MarketDiscoveryError

    settings = DiscoverySettings.from_env()
    output_path = os.getenv("PREMARKET_DISCOVERY_OUTPUT_PATH", "/app/paper-data/premarket-watchlist.json")
    try:
        result = AlpacaMarketDiscovery(settings).run_premarket(output_path)
        LOGGER.info(
            "Opening research completed: qualified=%s long=%s short_research=%s config=%s",
            result["qualified_count"], len(result["lanes"]["long"]),
            len(result["lanes"]["short_research_only"]), result["config_hash"],
        )
    except MarketDiscoveryError as exc:
        LOGGER.error("Opening research failed closed: %s", exc)
    except Exception as exc:
        LOGGER.error("Opening research failed closed: %s", type(exc).__name__)


def stop_loop(*_args):
    global SHOULD_RUN
    SHOULD_RUN = False


def main():
    heartbeat = HeartbeatWriter(os.getenv("HEARTBEAT_PATH", "/app/data/scheduler_heartbeat"))
    
    # Run once on startup so Redis is populated immediately
    check_market_hours()
    take_account_snapshot()

    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(check_market_hours, "interval", seconds=30)
    scheduler.add_job(check_engine_heartbeat, "interval", seconds=30)
    
    # Take account snapshot every 5 minutes (reduced from 30s to prevent DB bloat)
    scheduler.add_job(take_account_snapshot, "interval", minutes=5)

    # Weekly Database Compaction & Snapshot Pruning (Sundays at 00:00 ET)
    scheduler.add_job(cleanup_database, "cron", day_of_week="sun", hour=0, minute=0, timezone="America/New_York")

    if os.getenv("DISCOVERY_SCHEDULER_ENABLED", "false").lower() == "true":
        run_market_discovery()
        # SIP screeners provide the broad candidate set without polling IEX across
        # the whole universe. Begin at 10:00 ET and retry twice. Once a passing
        # current-session artifact exists, run_market_discovery freezes it.
        discovery_hour = int(os.getenv("DISCOVERY_SCHEDULE_HOUR", "10"))
        discovery_minutes = os.getenv("DISCOVERY_SCHEDULE_MINUTES", "0,5,10")
        scheduler.add_job(
            run_market_discovery,
            "cron",
            hour=discovery_hour,
            minute=discovery_minutes,
            day_of_week="mon-fri",
            timezone="America/New_York",
        )
    if os.getenv("PREMARKET_DISCOVERY_ENABLED", "false").lower() == "true":
        scheduler.add_job(
            run_premarket_discovery,
            "cron",
            hour=int(os.getenv("PREMARKET_DISCOVERY_HOUR", "9")),
            minute=os.getenv("PREMARKET_DISCOVERY_MINUTES", "31,35"),
            day_of_week="mon-fri",
            timezone="America/New_York",
        )

    # Daily performance report cron job
    report_hour = int(os.getenv("DAILY_REPORT_HOUR", "17"))
    report_minute = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
    report_tz = os.getenv("DAILY_REPORT_TIMEZONE", "America/New_York")
    scheduler.add_job(
        send_daily_performance_report,
        "cron",
        hour=report_hour,
        minute=report_minute,
        timezone=report_tz,
    )


    if os.getenv("RUN_REPORT_ON_STARTUP", "false").lower() == "true":
        send_daily_performance_report()

    signal.signal(signal.SIGTERM, stop_loop)
    signal.signal(signal.SIGINT, stop_loop)

    heartbeat.start()
    scheduler.start()
    LOGGER.info("Scheduler started")

    try:
        while SHOULD_RUN:
            time.sleep(1)
    finally:
        scheduler.shutdown(wait=False)
        heartbeat.stop()
        LOGGER.info("Scheduler stopped")


if __name__ == "__main__":
    main()
