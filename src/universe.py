"""The investable universe.

Loaded from `state/universe.yaml`. Three tiers:
- large_cap, mid_cap   — Core sleeve candidates (also eligible for Aggressive)
- curated_small_first_north — Aggressive sleeve candidates

Phase 2 ships with hand-maintained lists. Once Borsdata API is wired up
we can auto-refresh the large_cap and mid_cap tiers from the official
exchange segmentation; the curated list stays manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from src.config import STATE_DIR

UNIVERSE_FILE = STATE_DIR / "universe.yaml"


class Tier(str, Enum):
    LARGE_CAP = "large_cap"
    MID_CAP = "mid_cap"
    SMALL_CAP = "small_cap"
    FIRST_NORTH = "first_north"
    CURATED_SMALL = "curated_small_first_north"  # legacy hand-list, augments auto-refresh
    US_LARGE_CAP = "us_large_cap"  # curated US large-caps (USD); see CLAUDE.md §2


# Tiers maintained by hand in universe.yaml (not auto-refreshed from Börsdata's
# Nordic markets). These "benefit from human taste" and carry their own currency.
CURATED_TIERS = (Tier.CURATED_SMALL, Tier.US_LARGE_CAP)


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    name: str
    sector: str
    tier: Tier
    rationale: str | None = None  # only set for curated entries
    currency: str = "SEK"  # native trading currency (USD for US names)


def _default_currency(tier: Tier, explicit: str | None) -> str:
    if explicit:
        return explicit.upper()
    return "USD" if tier == Tier.US_LARGE_CAP else "SEK"


def load_universe(path: Path = UNIVERSE_FILE) -> list[UniverseEntry]:
    """Flat list across all tiers."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries: list[UniverseEntry] = []
    for tier in Tier:
        for row in data.get(tier.value, []) or []:
            entries.append(
                UniverseEntry(
                    ticker=row["ticker"],
                    name=row["name"],
                    sector=row.get("sector", "Unknown"),
                    tier=tier,
                    rationale=row.get("rationale"),
                    currency=_default_currency(tier, row.get("currency")),
                )
            )
    return entries


def by_tier(entries: list[UniverseEntry]) -> dict[Tier, list[UniverseEntry]]:
    out: dict[Tier, list[UniverseEntry]] = {t: [] for t in Tier}
    for e in entries:
        out[e.tier].append(e)
    return out


def find(ticker: str, entries: list[UniverseEntry] | None = None) -> UniverseEntry | None:
    entries = entries or load_universe()
    for e in entries:
        if e.ticker == ticker:
            return e
    return None
