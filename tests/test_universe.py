"""Sanity tests on the universe file: no dupes, valid tiers, sensible counts."""

from __future__ import annotations

from collections import Counter

from src.universe import Tier, load_universe


def test_loads_and_has_entries():
    entries = load_universe()
    assert len(entries) >= 50, f"Universe surprisingly small: {len(entries)}"


def test_no_duplicate_tickers():
    entries = load_universe()
    counts = Counter(e.ticker for e in entries)
    dupes = {t: c for t, c in counts.items() if c > 1}
    assert not dupes, f"Duplicate tickers: {dupes}"


def test_every_entry_has_sector():
    entries = load_universe()
    missing = [e.ticker for e in entries if not e.sector or e.sector == "Unknown"]
    assert not missing, f"Tickers missing sector: {missing}"


def test_curated_entries_have_rationale():
    entries = load_universe()
    bad = [
        e.ticker
        for e in entries
        if e.tier == Tier.CURATED_SMALL and not e.rationale
    ]
    assert not bad, f"Curated entries without rationale: {bad}"
