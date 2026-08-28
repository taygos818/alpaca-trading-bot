import os


def alpaca_credentials(paper_trade: bool) -> tuple[str, str]:
    if not paper_trade:
        raise ValueError("alpaca-trading-bot is paper-only; live credentials are rejected")
    return os.getenv("ALPACA_API_KEY", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
