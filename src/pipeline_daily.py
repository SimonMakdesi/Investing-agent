"""Daily light cycle (Mon-Fri).

Scans for material events on held positions + watchlist names. Flags only —
no trades. Emails the user only when something material happens (or with
--always-email for testing).

Run:
    uv run python -m src.pipeline_daily             # default: email only if flags
    uv run python -m src.pipeline_daily --always-email
    uv run python -m src.pipeline_daily --no-email
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date

from src.claude_client import call_role
from src.config import REPORTS_DIR, THESES_FILE
from src.contexts import event_monitor_user_message
from src.data.insiders import fetch_recent
from src.data.prices import get_history, get_latest_closes
from src.issuer_match import index_by_ticker
from src.json_parse import JsonExtractError, extract_json
from src.portfolio import Portfolio
from src.reporting import send_email
from src.universe import load_universe

log = logging.getLogger(__name__)

# Tickers moving by at least this much (in either direction) over the last
# trading day are flagged to the Event Monitor for evaluation.
MOVER_THRESHOLD_PCT = 5.0

# Insider transaction value above which we always flag, even if small in count.
LARGE_INSIDER_VALUE_SEK = 1_000_000


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def parse_watchlist(journal_text: str) -> list[str]:
    """Pull tickers from the Watchlist section of the journal.

    Looks for headings like '## Watchlist' or '## 3. Watchlist', then
    extracts any string matching the Yahoo .ST ticker pattern from the
    section body until the next ## heading.
    """
    if not journal_text:
        return []
    m = re.search(
        r"^##\s*\d*\.?\s*Watchlist\s*\n(.*?)(?=^##\s|\Z)",
        journal_text,
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    if not m:
        return []
    section = m.group(1)
    tickers = re.findall(r"\b[A-Z][A-Z0-9-]*\.ST\b", section)
    # Dedupe while preserving order
    return list(dict.fromkeys(tickers))


def compute_daily_movers(
    tickers: list[str], threshold_pct: float = MOVER_THRESHOLD_PCT
) -> list[tuple[str, float]]:
    """Return (ticker, daily_change_pct) for tickers that moved >= threshold
    versus the previous trading day's close."""
    movers: list[tuple[str, float]] = []
    for t in tickers:
        hist = get_history(t, days=10)
        if hist.empty or "Close" not in hist.columns or len(hist) < 2:
            continue
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        if prev <= 0:
            continue
        change_pct = (last / prev - 1.0) * 100.0
        if abs(change_pct) >= threshold_pct:
            movers.append((t, change_pct))
    return movers


def _build_daily_report(
    today: date,
    em_json: dict,
    em_full_text: str,
    monitored: list[str],
    insiders_today: list,
    movers: list[tuple[str, float]],
    token_usage: dict,
) -> str:
    flags = em_json.get("flags", []) or []
    summary = em_json.get("summary", "(no summary)")

    if flags:
        flag_lines = []
        for f in flags:
            sev = (f.get("severity", "info") or "info").upper()
            icon = {"INFO": "ℹ️", "WATCH": "👀", "URGENT": "🚨"}.get(sev, "•")
            flag_lines.append(
                f"- {icon} **{sev}** {f.get('ticker', '?')} ({f.get('name', '?')}) "
                f"— `{f.get('kind', '?')}`: {f.get('detail', '?')}"
            )
        flags_block = "\n".join(flag_lines)
    else:
        flags_block = "_(no flags — nothing material today)_"

    if movers:
        movers_block = "\n".join(f"- **{t}** {pct:+.1f}%" for t, pct in movers)
    else:
        movers_block = f"_(no held/watchlist tickers moved ≥ {MOVER_THRESHOLD_PCT:.0f}%)_"

    if insiders_today:
        ins_lines = [
            f"- {t.publication_date} **{t.issuer[:36]}** — {t.person[:24]} "
            f"({t.transaction_type}) — {t.total_value_sek:,.0f} SEK"
            for t in insiders_today[:15]
        ]
        if len(insiders_today) > 15:
            ins_lines.append(f"- _... and {len(insiders_today) - 15} more_")
        insiders_block = "\n".join(ins_lines)
    else:
        insiders_block = "_(no insider transactions on monitored names today)_"

    cost = _estimate_cost(token_usage)

    return f"""# Daily Event Monitor — {today.isoformat()}

## Summary
{summary}

## Flags ({len(flags)})
{flags_block}

## Large price moves (≥ {MOVER_THRESHOLD_PCT:.0f}%)
{movers_block}

## Insider activity on monitored names today
{insiders_block}

## Event Monitor reasoning
{em_full_text}

---
*Monitored: {len(monitored)} tickers. Cost: ${cost:.3f}.*
"""


def _estimate_cost(usage: dict) -> float:
    in_ = usage.get("input", 0)
    out = usage.get("output", 0)
    cr = usage.get("cache_read", 0)
    cc = usage.get("cache_create", 0)
    # Daily uses Sonnet only: ~$3 in, $15 out per 1M
    return (in_ * 3 + out * 15 + cr * 0.3 + cc * 3.75) / 1_000_000


def run(send_email_flag: bool, always_email: bool) -> int:
    _setup_logging()
    today = date.today()
    log.info("=== Daily cycle start — %s ===", today.isoformat())

    portfolio = Portfolio.load()
    universe = load_universe()
    journal = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""

    watchlist_tickers = parse_watchlist(journal)
    held_tickers = list(portfolio.holdings.keys())
    monitored = list(dict.fromkeys(held_tickers + watchlist_tickers))
    log.info(
        "Monitoring %d tickers (%d held + %d watchlist): %s",
        len(monitored), len(held_tickers), len(watchlist_tickers), monitored,
    )

    if not monitored:
        log.info("Nothing to monitor (no holdings, no watchlist). Exiting clean.")
        return 0

    # Prices for monitored tickers
    snapshots = get_latest_closes(monitored)
    prices = {t: s.close for t, s in snapshots.items()}

    # Insider activity today (filter to monitored)
    log.info("Fetching FI insider transactions for the last 2 days ...")
    insiders_recent = fetch_recent(days_back=2)
    insiders_by_ticker = index_by_ticker(insiders_recent, universe)
    insiders_today: list = []
    for t in monitored:
        insiders_today.extend(insiders_by_ticker.get(t, []))
    log.info("Found %d insider transactions on monitored names", len(insiders_today))

    # Large price movers
    log.info("Checking for daily price moves >= %.0f%% on monitored names ...", MOVER_THRESHOLD_PCT)
    movers = compute_daily_movers(monitored)
    log.info("Large movers: %s", movers)

    # Skip the Claude call entirely if nothing happened. Saves tokens on quiet days.
    has_material_input = bool(insiders_today) or bool(movers)
    if not has_material_input and not always_email:
        log.info("No material inputs today (no insider txs, no large moves). Skipping Claude call and email.")
        # Still write a tiny report file for audit / future reference
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / f"daily_{today.isoformat()}.md"
        path.write_text(
            f"# Daily Event Monitor — {today.isoformat()}\n\nNothing material today.\n",
            encoding="utf-8",
        )
        return 0

    # Event Monitor call
    msg = event_monitor_user_message(
        today=today,
        portfolio=portfolio,
        prices=prices,
        journal=journal,
        insiders_today=insiders_today,
        large_movers=movers,
    )
    resp = call_role("event_monitor", msg)
    usage = {
        "input": resp.input_tokens,
        "output": resp.output_tokens,
        "cache_read": resp.cache_read_tokens,
        "cache_create": resp.cache_creation_tokens,
    }
    try:
        em_json = extract_json(resp.text)
    except JsonExtractError as e:
        log.warning("Event Monitor JSON parse failed: %s", e)
        em_json = {"summary": "Event Monitor JSON parse failed.", "flags": []}

    flags = em_json.get("flags", []) or []
    has_urgent = any((f.get("severity") or "").lower() == "urgent" for f in flags)
    log.info(
        "Event Monitor returned %d flags (urgent=%s)", len(flags), has_urgent,
    )

    # Build report
    report_md = _build_daily_report(
        today=today, em_json=em_json, em_full_text=resp.text,
        monitored=monitored, insiders_today=insiders_today, movers=movers,
        token_usage=usage,
    )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"daily_{today.isoformat()}.md"
    path.write_text(report_md, encoding="utf-8")
    log.info("Report written to %s", path)

    # Email only if there are flags (or --always-email).
    # This keeps the daily inbox quiet — emails only when material.
    should_email = send_email_flag and (always_email or flags)
    if should_email:
        subject_prefix = "[Investing Agent URGENT]" if has_urgent else "[Investing Agent]"
        subject = f"{subject_prefix} Daily check {today.isoformat()} — {len(flags)} flag(s)"
        send_email(subject=subject, body_markdown=report_md)
    else:
        log.info("No flags to email (or --no-email). Skipping email.")

    log.info("=== Daily cycle complete ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily event-monitor cycle")
    parser.add_argument(
        "--always-email", action="store_true",
        help="Send an email even when there are no flags (useful for testing).",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Never send an email (just write the report file).",
    )
    args = parser.parse_args()
    return run(send_email_flag=not args.no_email, always_email=args.always_email)


if __name__ == "__main__":
    sys.exit(main())
