"""Issuer-name matching tests against real FI naming patterns."""

from __future__ import annotations

from src.issuer_match import _normalize, matches_universe
from src.universe import Tier, UniverseEntry


def _entry(ticker: str, name: str) -> UniverseEntry:
    return UniverseEntry(ticker=ticker, name=name, sector="X", tier=Tier.LARGE_CAP)


def test_normalize_strips_share_class_and_legal_entity():
    assert _normalize("H&M B") == "h m"
    assert _normalize("Hexagon Aktiebolag") == "hexagon"
    assert _normalize("Essity Aktiebolag (publ)") == "essity"
    assert _normalize("Volvo, AB") == "volvo"
    assert _normalize("Atlas Copco A") == "atlas copco"


def test_alias_match_hits():
    # Ericsson short name should match the long legal name via alias.
    assert matches_universe(
        "Telefonaktiebolaget LM Ericsson",
        _entry("ERIC-B.ST", "Ericsson B"),
    )


def test_alias_match_hm():
    assert matches_universe(
        "H & M Hennes & Mauritz AB",
        _entry("HM-B.ST", "H&M B"),
    )


def test_fuzzy_match_simple_case():
    assert matches_universe(
        "Hexagon Aktiebolag",
        _entry("HEXA-B.ST", "Hexagon B"),
    )


def test_fuzzy_match_with_publ_suffix():
    assert matches_universe(
        "Essity Aktiebolag (publ)",
        _entry("ESSITY-B.ST", "Essity B"),
    )


def test_no_false_positive_unrelated_company():
    # "Doxa" should NOT match Investor AB.
    assert not matches_universe(
        "Doxa AB",
        _entry("INVE-B.ST", "Investor B"),
    )


def test_atlas_copco_matches_both_share_classes():
    assert matches_universe("Atlas Copco AB", _entry("ATCO-A.ST", "Atlas Copco A"))
    assert matches_universe("Atlas Copco AB", _entry("ATCO-B.ST", "Atlas Copco B"))
