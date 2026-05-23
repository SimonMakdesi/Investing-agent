"""Manual smoke test: fetch latest closes for a handful of Swedish stocks.

Usage:
    uv run python scripts/smoke_test_prices.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from src.data.prices import get_latest_closes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SAMPLE_TICKERS = [
    "VOLV-B.ST",   # Volvo B
    "ERIC-B.ST",   # Ericsson B
    "HM-B.ST",     # H&M B
    "INVE-B.ST",   # Investor B
    "ATCO-A.ST",   # Atlas Copco A
    "HEXA-B.ST",   # Hexagon B
]


def main() -> None:
    print(f"Fetching latest closes for {len(SAMPLE_TICKERS)} tickers...\n")
    snapshots = get_latest_closes(SAMPLE_TICKERS)
    for ticker in SAMPLE_TICKERS:
        snap = snapshots.get(ticker)
        if snap:
            print(f"  {ticker:12s}  {snap.close:>10,.2f} {snap.currency}   as of {snap.as_of}")
        else:
            print(f"  {ticker:12s}  FAILED")
    print(f"\n{len(snapshots)}/{len(SAMPLE_TICKERS)} succeeded.")


if __name__ == "__main__":
    main()
