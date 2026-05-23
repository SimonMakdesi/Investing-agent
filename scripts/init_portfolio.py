"""Create state/portfolio.json with the starting cash balance.

Usage:
    uv run python scripts/init_portfolio.py

Refuses to overwrite an existing portfolio (pass --force to override).
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from datetime import datetime

from src.config import PORTFOLIO_FILE, STOCKHOLM_TZ
from src.portfolio import Portfolio

INITIAL_CAPITAL_SEK = 100_000.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Overwrite existing portfolio.json")
    args = parser.parse_args()

    if PORTFOLIO_FILE.exists() and not args.force:
        print(f"Refusing to overwrite existing {PORTFOLIO_FILE}. Use --force to override.")
        return 1

    now = datetime.now(tz=STOCKHOLM_TZ)
    portfolio = Portfolio(
        cash_sek=INITIAL_CAPITAL_SEK,
        holdings={},
        inception_date=now,
        initial_capital_sek=INITIAL_CAPITAL_SEK,
    )
    portfolio.save()
    print(f"Created {PORTFOLIO_FILE} with {INITIAL_CAPITAL_SEK:,.0f} SEK cash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
