"""Auto-refresh the Large + Mid Cap tiers of the investable universe from Börsdata.

Replaces hand-maintaining `state/universe.yaml` for those tiers — whenever a
company IPOs onto Large/Mid Cap, graduates between tiers, or gets delisted,
the change shows up automatically on the next weekly cycle.

The curated_small_first_north tier in the YAML is preserved untouched —
that tier benefits from human taste.

If Börsdata is unreachable (network error, missing key, etc.), the caller
falls back to the full hand-maintained YAML — same behavior as before
this module existed.
"""

from __future__ import annotations

import logging

from src.data.borsdata import BorsdataClient, BorsdataError
from src.universe import CURATED_TIERS, Tier, UniverseEntry, load_universe

log = logging.getLogger(__name__)

# Börsdata market IDs (countryId=1 = Sweden)
MARKET_LARGE_CAP = 1
MARKET_MID_CAP = 2
MARKET_SMALL_CAP = 3
MARKET_FIRST_NORTH = 4

_MARKET_TO_TIER = {
    MARKET_LARGE_CAP: Tier.LARGE_CAP,
    MARKET_MID_CAP: Tier.MID_CAP,
    MARKET_SMALL_CAP: Tier.SMALL_CAP,
    MARKET_FIRST_NORTH: Tier.FIRST_NORTH,
}

# Börsdata sector ID -> our standard English sector name.
SECTOR_MAP: dict[int, str] = {
    1: "Financials",            # split into Real Estate below via branch
    2: "Consumer Staples",      # Dagligvaror
    3: "Energy",                # Energi
    4: "Healthcare",            # Hälsovård
    5: "Industrials",           # Industri
    6: "Technology",            # Informationsteknik
    7: "Materials",             # Material
    8: "Consumer Discretionary",# Sällanköpsvaror
    9: "Communication",         # Telekommunikation
    10: "Utilities",            # Kraftförsörjning
}

# Real-estate branches under Finans & Fastighet — bumped out to "Real Estate"
REAL_ESTATE_BRANCH_IDS = {75, 76}  # Fastighetsbolag, Fastighet - REIT


def _classify_sector(sector_id: int, branch_id: int) -> str:
    if branch_id in REAL_ESTATE_BRANCH_IDS:
        return "Real Estate"
    return SECTOR_MAP.get(sector_id, "Unknown")


def refresh_from_markets(
    client: BorsdataClient,
    market_ids: tuple[int, ...] = (
        MARKET_LARGE_CAP, MARKET_MID_CAP, MARKET_SMALL_CAP, MARKET_FIRST_NORTH,
    ),
) -> list[UniverseEntry]:
    """Fetch all Swedish instruments from the given Börsdata markets and return
    them as UniverseEntry objects with appropriate Tier mapping."""
    instruments = client.instruments
    out: list[UniverseEntry] = []
    per_tier: dict[Tier, int] = {}
    for inst in instruments:
        if inst.country_id != 1:
            continue
        if inst.market_id not in market_ids:
            continue
        if not inst.yahoo_ticker:
            continue
        tier = _MARKET_TO_TIER.get(inst.market_id)
        if tier is None:
            continue
        sector = _classify_sector(inst.sector_id, inst.branch_id)
        out.append(UniverseEntry(
            ticker=inst.yahoo_ticker,
            name=inst.name,
            sector=sector,
            tier=tier,
        ))
        per_tier[tier] = per_tier.get(tier, 0) + 1
    log.info(
        "Börsdata auto-refresh: %d names total (%s)",
        len(out),
        ", ".join(f"{t.value}={n}" for t, n in per_tier.items()),
    )
    return out


# Backwards-compat alias for the previous Large+Mid-only fetcher.
def refresh_large_mid_cap(client: BorsdataClient) -> list[UniverseEntry]:
    return refresh_from_markets(client, (MARKET_LARGE_CAP, MARKET_MID_CAP))


def merged_universe(client: BorsdataClient | None = None) -> list[UniverseEntry]:
    """Auto-refreshed Large + Mid Cap (Börsdata) + curated Small/First North (YAML).

    If `client` is None or Börsdata fails, falls back to the hand-maintained
    YAML for all tiers — preserving the old behavior.
    """
    yaml_universe = load_universe()

    if client is None:
        log.info("Universe: no Börsdata client provided — using hand-maintained YAML (%d names)",
                 len(yaml_universe))
        return yaml_universe

    try:
        auto_full = refresh_from_markets(client)
    except BorsdataError as e:
        log.warning("Universe auto-refresh failed (%s) — falling back to YAML", e)
        return yaml_universe

    # Hand-maintained tiers (curated Nordic small/first-north + curated US large-caps)
    # are added from YAML; the Nordic Large/Mid/Small/First-North tiers come from
    # the Börsdata auto-refresh above.
    curated = [e for e in yaml_universe if e.tier in CURATED_TIERS]
    merged = auto_full + curated

    # Dedupe by ticker (auto-refreshed wins on conflict)
    seen: set[str] = set()
    deduped: list[UniverseEntry] = []
    for e in merged:
        if e.ticker in seen:
            continue
        seen.add(e.ticker)
        deduped.append(e)

    log.info(
        "Universe (auto + curated): %d names total", len(deduped),
    )
    return deduped
