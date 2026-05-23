"""Manual smoke test: fetch recent insider transactions from FI.

Usage:
    uv run python scripts/smoke_test_insiders.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from src.data.insiders import fetch_recent, filter_significant_buys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    print("Fetching insider transactions from FI (last 7 days)...\n")
    txs = fetch_recent(days_back=7)
    print(f"Total transactions: {len(txs)}\n")

    significant = filter_significant_buys(txs, min_value_sek=500_000)
    print(f"Significant buys (>=500k SEK): {len(significant)}\n")

    for tx in significant[:15]:
        print(
            f"  {tx.publication_date}  {tx.issuer[:35]:35s}  "
            f"{tx.person[:25]:25s}  {tx.total_value_sek:>12,.0f} SEK"
        )
    if len(significant) > 15:
        print(f"  ... and {len(significant) - 15} more")


if __name__ == "__main__":
    main()
