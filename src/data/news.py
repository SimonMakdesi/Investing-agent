"""News fetching, caching, and classification.

Pipeline:
1. Fetch Google News RSS for each ticker (one query per ticker, free, no auth)
2. Parse items: title, link, source, published_at, snippet
3. Dedupe vs cache (by URL hash) so each article is processed exactly once
4. For new items, send to Sonnet for {summary, materiality 1-5, sentiment}
5. Cache the classified item to state/news/<ticker>/<YYYY-MM>/<urlhash>.json
6. Helpers to format compact news blocks for Analyst/Event Monitor/daily pulse

Materiality scale (in the classifier prompt):
1 — boilerplate (voting rights, share registries, routine filings)
2 — minor (small contract, personnel change at non-C-suite level)
3 — noteworthy (quarterly print, buyback, secondary placement, M&A in industry)
4 — material (profit warning, major M&A, CEO/CFO change, regulatory action)
5 — critical (going concern, major guidance cut, fraud allegations, halt)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
import yaml

from src.claude_client import call_lightweight
from src.config import STATE_DIR
from src.json_parse import JsonExtractError, extract_json
from src.universe import UniverseEntry

log = logging.getLogger(__name__)

NEWS_DIR = STATE_DIR / "news"
ALIAS_FILE = STATE_DIR / "news_aliases.yaml"
GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"
USER_AGENT = "investing-agent/0.2 (personal research, single user)"
FETCH_TIMEOUT = 15

# Cap how many items we keep / classify per ticker per fetch, to avoid runaway
# token spend if a name suddenly has a lot of coverage.
MAX_CLASSIFY_PER_TICKER_PER_RUN = 8

# Sonnet input is roughly $3/Mtok and we send ~250 tokens per article; output
# is ~80 tokens. So ~$0.0008 per article. Materiality cap above keeps cost
# bounded per ticker.

CLASSIFIER_SYSTEM = """You are a fast triage classifier for Swedish equity news headlines. \
Read the headline and snippet, then output a single JSON object inside a ```json fence:

{
  "summary": "one short sentence in English, max 200 chars, plain English not jargon",
  "materiality": 1-5,
  "sentiment": "positive" | "neutral" | "negative",
  "kind": "report" | "guidance" | "ma" | "insider" | "leadership" | "regulatory" | "operational" | "industry" | "other"
}

Materiality:
1 — boilerplate (voting rights, share registries, routine notifications, name changes)
2 — minor (small contract, junior personnel changes, niche awards)
3 — noteworthy (quarterly report results, share buyback, secondary placement, sector M&A)
4 — material (profit warning, major M&A involving this company, CEO/CFO change, regulatory action, significant guidance update)
5 — critical (going concern, major guidance cut, fraud / investigation, trading halt, major scandal)

Be terse. The summary is read alongside dozens of others; aim for "what changed and why it matters" in <200 chars.
No prose outside the JSON fence."""


# --- Data model ---------------------------------------------------------

@dataclass
class NewsItem:
    ticker: str
    url: str
    url_hash: str
    title: str
    source: str
    published_at_iso: str  # ISO UTC
    snippet: str

    # Classification fields (filled in after classifier runs)
    summary: str | None = None
    materiality: int | None = None
    sentiment: str | None = None
    kind: str | None = None

    @property
    def published_at(self) -> datetime:
        return datetime.fromisoformat(self.published_at_iso)


# --- Alias loading -------------------------------------------------------

_aliases_cache: dict[str, str] | None = None


def _load_aliases() -> dict[str, str]:
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache
    if not ALIAS_FILE.exists():
        _aliases_cache = {}
    else:
        _aliases_cache = yaml.safe_load(ALIAS_FILE.read_text(encoding="utf-8")) or {}
    return _aliases_cache


def search_query_for(entry: UniverseEntry) -> str:
    """Return the Google News search query for a ticker. Alias file wins;
    otherwise the company name in double quotes."""
    aliases = _load_aliases()
    if entry.ticker in aliases:
        return aliases[entry.ticker]
    return f'"{entry.name}"'


# --- Cache layout --------------------------------------------------------

def _cache_path(ticker: str, url: str, published: datetime) -> Path:
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    month = published.strftime("%Y-%m")
    return NEWS_DIR / ticker / month / f"{url_hash}.json"


def _save_item(item: NewsItem) -> None:
    path = _cache_path(item.ticker, item.url, item.published_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(item), ensure_ascii=False, indent=2), encoding="utf-8")


def load_cached_items(ticker: str, since: datetime | None = None) -> list[NewsItem]:
    """All cached items for a ticker, optionally filtered by `published_at >= since`."""
    ticker_dir = NEWS_DIR / ticker
    if not ticker_dir.exists():
        return []
    out: list[NewsItem] = []
    for month_dir in sorted(ticker_dir.iterdir()):
        if not month_dir.is_dir():
            continue
        for f in month_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                item = NewsItem(**data)
            except Exception as e:
                log.warning("Skipping malformed news cache file %s: %s", f, e)
                continue
            if since is not None and item.published_at < since:
                continue
            out.append(item)
    out.sort(key=lambda it: it.published_at, reverse=True)
    return out


# --- Google News RSS fetch + parse --------------------------------------

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_feed(xml_text: str, ticker: str) -> list[NewsItem]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("News feed parse failed for %s: %s", ticker, e)
        return []

    out: list[NewsItem] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else "?"

        if not title or not link:
            continue

        try:
            published_dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            published_dt = datetime.now(timezone.utc)

        url_hash = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]

        out.append(NewsItem(
            ticker=ticker,
            url=link,
            url_hash=url_hash,
            title=_strip_html(title),
            source=source,
            published_at_iso=published_dt.isoformat(),
            snippet=_strip_html(desc),
        ))
    return out


def fetch_news_for(entry: UniverseEntry, hl: str = "sv", gl: str = "SE") -> list[NewsItem]:
    """Fetch the current Google News RSS items for one ticker. Returns parsed
    items in publication order (newest first). Does NOT yet hit the classifier."""
    q = search_query_for(entry)
    params = {"q": q, "hl": hl, "gl": gl, "ceid": f"{gl}:{hl}"}
    url = f"{GOOGLE_NEWS_BASE}?{urllib.parse.urlencode(params)}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
    except requests.RequestException as e:
        log.warning("News fetch failed for %s: %s", entry.ticker, e)
        return []
    if r.status_code != 200:
        log.warning("News fetch HTTP %d for %s", r.status_code, entry.ticker)
        return []
    return _parse_feed(r.text, entry.ticker)


# --- Classification ------------------------------------------------------

def _classify_one(item: NewsItem) -> NewsItem:
    """Run Sonnet on the headline + snippet. Returns the same NewsItem with
    classification fields filled in. On any failure, falls back to materiality=2,
    sentiment=neutral so downstream code still sees a usable item."""
    user_msg = (
        f"Ticker: {item.ticker}\n"
        f"Source: {item.source}\n"
        f"Published: {item.published_at.date().isoformat()}\n"
        f"Headline: {item.title}\n"
        f"Snippet: {item.snippet[:600]}"
    )
    try:
        text, _ = call_lightweight(
            system=CLASSIFIER_SYSTEM,
            user=user_msg,
            label=f"news/{item.ticker}",
            max_tokens=300,
        )
        parsed = extract_json(text)
        item.summary = (parsed.get("summary") or item.title)[:240]
        m = parsed.get("materiality", 2)
        item.materiality = int(m) if isinstance(m, (int, float, str)) and str(m).isdigit() else 2
        item.materiality = max(1, min(5, item.materiality))
        item.sentiment = (parsed.get("sentiment") or "neutral").lower()
        if item.sentiment not in {"positive", "neutral", "negative"}:
            item.sentiment = "neutral"
        item.kind = (parsed.get("kind") or "other").lower()
    except (JsonExtractError, Exception) as e:  # noqa: BLE001
        log.warning("Classifier failed for %s (%s): %s", item.ticker, item.url, e)
        item.summary = item.title
        item.materiality = 2
        item.sentiment = "neutral"
        item.kind = "other"
    return item


# --- Public per-ticker entrypoint ---------------------------------------

def fetch_and_classify(
    entry: UniverseEntry,
    max_new: int = MAX_CLASSIFY_PER_TICKER_PER_RUN,
) -> tuple[list[NewsItem], int, int]:
    """For one ticker: fetch Google News RSS, dedupe vs cache, classify new items.

    Returns (all_classified_items_in_cache, n_new_fetched, n_classified).
    """
    raw = fetch_news_for(entry)
    if not raw:
        return load_cached_items(entry.ticker), 0, 0

    # Dedupe against cache
    existing_hashes = set()
    cache_dir = NEWS_DIR / entry.ticker
    if cache_dir.exists():
        for f in cache_dir.rglob("*.json"):
            existing_hashes.add(f.stem)

    new_items = [it for it in raw if it.url_hash not in existing_hashes]
    new_items = new_items[:max_new]  # cap per-run cost

    log.info(
        "News %s: %d in feed, %d new (classifying up to %d)",
        entry.ticker, len(raw), len(new_items), max_new,
    )

    classified = 0
    for item in new_items:
        item = _classify_one(item)
        _save_item(item)
        classified += 1

    return load_cached_items(entry.ticker), len(new_items), classified


def fetch_and_classify_many(
    entries: list[UniverseEntry],
    max_new_per_ticker: int = MAX_CLASSIFY_PER_TICKER_PER_RUN,
) -> dict[str, list[NewsItem]]:
    """Bulk version. Returns dict: ticker -> all cached items for that ticker
    (after refresh)."""
    out: dict[str, list[NewsItem]] = {}
    total_fetched = total_classified = 0
    for entry in entries:
        items, n_new, n_cls = fetch_and_classify(entry, max_new=max_new_per_ticker)
        out[entry.ticker] = items
        total_fetched += n_new
        total_classified += n_cls
    log.info(
        "News batch complete: %d tickers, %d new fetched, %d classified",
        len(entries), total_fetched, total_classified,
    )
    return out


# --- Formatting helpers (for prompts and emails) ------------------------

def format_for_analyst(items: list[NewsItem], since_days: int = 30, min_materiality: int = 3) -> str:
    """Compact news block for the Analyst's per-pick context."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    relevant = [
        it for it in items
        if it.published_at >= cutoff and (it.materiality or 0) >= min_materiality
    ]
    if not relevant:
        return f"  (no notable news in last {since_days}d at materiality {min_materiality}+)"

    lines = []
    for it in relevant[:12]:
        date_str = it.published_at.date().isoformat()
        sent = it.sentiment or "neutral"
        sent_glyph = {"positive": "+", "neutral": "·", "negative": "-"}.get(sent, "·")
        lines.append(
            f"  {date_str} [{(it.source or '?')[:18]:18s}] M{it.materiality}{sent_glyph} {it.summary or it.title}"
        )
    if len(relevant) > 12:
        lines.append(f"  ... and {len(relevant) - 12} more at materiality {min_materiality}+")
    return "\n".join(lines)


def materiality_score(items: list[NewsItem], since_days: int = 7) -> int:
    """Aggregate news 'heat' for a ticker — sum of materiality scores in the
    window. Useful as a one-number signal in the Screener's metric line."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    return sum((it.materiality or 0) for it in items if it.published_at >= cutoff)


def recent_high_materiality(
    items_by_ticker: dict[str, list[NewsItem]],
    since_days: int = 1,
    min_materiality: int = 3,
) -> list[tuple[str, NewsItem]]:
    """Find all recent material items across a set of tickers. Used by the
    daily pulse + event monitor."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    out: list[tuple[str, NewsItem]] = []
    for ticker, items in items_by_ticker.items():
        for it in items:
            if it.published_at >= cutoff and (it.materiality or 0) >= min_materiality:
                out.append((ticker, it))
    out.sort(key=lambda x: x[1].published_at, reverse=True)
    return out
