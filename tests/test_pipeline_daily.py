"""Unit tests for the daily pipeline helpers."""

from __future__ import annotations

from src.pipeline_daily import parse_watchlist


SAMPLE_JOURNAL = """# Journal — Investing Agent

## 1. Market view
Stuff.

## 2. Holdings
**HM-B.ST** — Core
- Thesis: x.

## 3. Watchlist

**SECU-B.ST** — Eight-person insider cluster. Conviction 3.
**ASSA-B.ST** — Global quality compounder, -15% from highs.
**NCC-B.ST** — Insider buys during a 14% drawdown.
**BILL.ST** — Qviberg ~13.4M SEK personal purchase.

## 4. Lessons learned
Stuff.
"""


def test_parse_watchlist_basic():
    out = parse_watchlist(SAMPLE_JOURNAL)
    assert out == ["SECU-B.ST", "ASSA-B.ST", "NCC-B.ST", "BILL.ST"]


def test_parse_watchlist_dedupes():
    journal_with_dupe = SAMPLE_JOURNAL.replace(
        "**ASSA-B.ST**",
        "**ASSA-B.ST** mentioned twice ASSA-B.ST",
    )
    out = parse_watchlist(journal_with_dupe)
    assert out.count("ASSA-B.ST") == 1


def test_parse_watchlist_empty_section():
    empty = """# Journal\n\n## Watchlist\n\n_(empty)_\n\n## Next\n"""
    assert parse_watchlist(empty) == []


def test_parse_watchlist_no_section():
    no_section = "# Journal\n\nSome text but no watchlist heading.\n"
    assert parse_watchlist(no_section) == []


def test_parse_watchlist_excludes_holdings_section():
    # HM-B.ST appears in Holdings, not Watchlist — should not be returned
    out = parse_watchlist(SAMPLE_JOURNAL)
    assert "HM-B.ST" not in out
