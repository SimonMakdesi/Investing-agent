"""Match Finansinspektionen issuer names to our universe tickers.

FI publishes issuer names as full legal entities ("H & M Hennes & Mauritz AB",
"Telefonaktiebolaget LM Ericsson") while our universe uses short common names
("H&M B", "Ericsson B"). This module bridges the two.

Strategy:
1. `state/issuer_aliases.yaml` provides authoritative ticker → substring patterns.
   Checked first. Use this for the messy cases (Ericsson, H&M, Industrivärden).
2. If no alias matches, a normalized fuzzy match is attempted: strip share-class
   suffixes, legal-entity tokens (AB, Aktiebolag, (publ), Group), and punctuation,
   then check whether the universe name's tokens appear (as a subsequence) in the
   issuer's normalized tokens.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from src.config import STATE_DIR
from src.data.insiders import InsiderTransaction
from src.universe import UniverseEntry

ALIAS_FILE = STATE_DIR / "issuer_aliases.yaml"

# Tokens stripped from both universe names and issuer names before matching.
_NOISE_TOKENS = {
    "ab", "aktiebolag", "publ", "group", "gruppen", "groups",
    "holding", "holdings", "inc", "plc", "asa", "oyj", "se",
    "company", "co", "corporation", "corp",
}
# Share-class single-letter suffixes ("Volvo B", "Investor B")
_SHARE_CLASS_RE = re.compile(r"\s+[abc]$", re.IGNORECASE)


def _strip_share_class(name: str) -> str:
    return _SHARE_CLASS_RE.sub("", name).strip()


def _normalize(name: str) -> str:
    s = name.lower()
    s = _strip_share_class(s)
    s = s.replace("&", " ")
    s = re.sub(r"\([^)]*\)", " ", s)  # (publ) etc.
    s = re.sub(r"[^a-z0-9\s]", " ", s)  # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [t for t in s.split() if t not in _NOISE_TOKENS]
    return " ".join(tokens)


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Are all `needle` tokens present in `haystack` in order (gaps allowed)?"""
    it = iter(haystack)
    return all(t in it for t in needle)


@lru_cache(maxsize=1)
def load_aliases() -> dict[str, list[str]]:
    if not ALIAS_FILE.exists():
        return {}
    raw = yaml.safe_load(ALIAS_FILE.read_text(encoding="utf-8")) or {}
    # Lowercase patterns once
    return {ticker: [p.lower() for p in patterns] for ticker, patterns in raw.items()}


def matches_universe(issuer_name: str, entry: UniverseEntry, aliases: dict[str, list[str]] | None = None) -> bool:
    """True if the FI issuer name should be associated with this universe entry."""
    aliases = aliases or load_aliases()
    issuer_lower = issuer_name.lower()

    # 1. Alias hit
    for pattern in aliases.get(entry.ticker, []):
        if pattern in issuer_lower:
            return True

    # 2. Fuzzy normalized match
    norm_issuer = _normalize(issuer_name)
    norm_entry = _normalize(entry.name)
    if not norm_entry or not norm_issuer:
        return False
    return _is_subsequence(norm_entry.split(), norm_issuer.split())


def index_by_ticker(
    transactions: list[InsiderTransaction],
    universe: list[UniverseEntry],
) -> dict[str, list[InsiderTransaction]]:
    """Group transactions by ticker, using alias + fuzzy matching.

    Replaces the naive `index_insiders_by_issuer` for cases where we need to
    look up "all insider activity on ticker X".
    """
    aliases = load_aliases()
    # Build a per-issuer cache of which tickers it matches (one issuer can match
    # multiple universe entries — e.g. both Atlas Copco A and B for "Atlas Copco AB").
    out: dict[str, list[InsiderTransaction]] = {e.ticker: [] for e in universe}
    issuer_to_tickers: dict[str, list[str]] = {}
    for tx in transactions:
        if tx.issuer not in issuer_to_tickers:
            issuer_to_tickers[tx.issuer] = [
                e.ticker for e in universe if matches_universe(tx.issuer, e, aliases)
            ]
        for ticker in issuer_to_tickers[tx.issuer]:
            out[ticker].append(tx)
    return out
