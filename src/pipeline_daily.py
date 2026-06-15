"""Daily light cycle (Mon-Fri).

Every weekday after market close, emits a "daily pulse" — a short Python-only
summary (portfolio P&L vs OMXS30, holdings + watchlist daily moves) and
regenerates the dashboard with fresh prices.

When material events are detected (insider activity on held/watchlist names,
or any ticker moving >=5%), the Event Monitor (Sonnet) is invoked to evaluate
and flag — same as before. The agent itself isn't running more often; we're
just adding a free Python summary so the inbox isn't silent on quiet days.

Run:
    uv run python -m src.pipeline_daily              # default: always emit pulse
    uv run python -m src.pipeline_daily --silent     # old behaviour: email only on flags
    uv run python -m src.pipeline_daily --no-email   # never email (build files only)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date

from src.claude_client import RoleResponse, call_role
from src.config import REPORTS_DIR, THESES_FILE
from src.contexts import (
    analyst_user_message,
    daily_pm_user_message,
    event_monitor_user_message,
)
from src.data.insiders import fetch_recent, filter_significant_buys
from src.data.borsdata import BorsdataClient, BorsdataError
from src.data.benchmark import blended_daily_change, regional_weights
from src.data.borsdata_insiders import fetch_summaries_for_universe, format_summary_for_analyst
from src.data.fundamentals import compute as compute_fundamentals
from src.data.fundamentals import format_for_analyst as format_fundamentals
from src.data.news import (
    fetch_and_classify,
    fetch_and_classify_many,
    format_for_analyst as format_news_compact,
    format_for_analyst as format_news_for_analyst,
    recent_high_materiality,
)
from src.data.prices import get_history, get_latest_closes_sek
from src.data.universe_refresh import merged_universe
from src.issuer_match import index_by_ticker
from src.json_parse import JsonExtractError, extract_json
from src.metrics import compute as compute_metrics
from src.portfolio import Portfolio, Sleeve
from src.reporting import send_email
from src.risk import Action, TradeProposal, check_trade
from src.universe import find

log = logging.getLogger(__name__)

MOVER_THRESHOLD_PCT = 5.0

# Daily-decision escalation thresholds.
# A market-wide insider buy must clear this to surface a *new* (not-yet-held)
# name as a buy candidate for the Daily PM. Matches the Event Monitor's 1M SEK bar.
SIGNIFICANT_INSIDER_SEK = 1_000_000
# Cap how many fresh names we deep-dive per day (each is one Opus Analyst call).
MAX_NEW_CANDIDATES = 3


def _setup_logging() -> None:
    # Force UTF-8 on stdout so Unicode glyphs like ▲ don't crash the Windows
    # cp1252 console. GitHub Actions runners are already UTF-8 so this is a no-op there.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def parse_watchlist(journal_text: str) -> list[str]:
    """Pull tickers from the Watchlist section of the journal."""
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
    return list(dict.fromkeys(tickers))


def daily_changes(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """For each ticker, return (latest_close, daily_pct_change_vs_prev_close).

    Used both for the "what moved meaningfully" filter and for the per-line
    daily pulse view of every monitored ticker.
    """
    out: dict[str, tuple[float, float]] = {}
    for t in tickers:
        hist = get_history(t, days=10)
        if hist.empty or "Close" not in hist.columns or len(hist) < 2:
            continue
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        if prev <= 0:
            continue
        out[t] = (last, (last / prev - 1.0) * 100.0)
    return out


def compute_daily_movers(
    changes: dict[str, tuple[float, float]],
    threshold_pct: float = MOVER_THRESHOLD_PCT,
) -> list[tuple[str, float]]:
    """Subset of `changes` where |daily move| >= threshold."""
    return [(t, pct) for t, (_, pct) in changes.items() if abs(pct) >= threshold_pct]


# --- Report builders ------------------------------------------------------

def _pulse_section(
    portfolio: Portfolio,
    prices: dict[str, float],
    changes: dict[str, tuple[float, float]],
    held_tickers: list[str],
    watchlist_tickers: list[str],
    bm_change_pct: float | None,
    bm_label: str = "OMXS30",
) -> str:
    """The 'where we stand' block. Pure Python, no AI."""
    # Portfolio current value
    current_value = portfolio.cash_sek + sum(
        h.shares * prices.get(h.ticker, h.avg_cost) for h in portfolio.holdings.values()
    )

    # Portfolio value at PREVIOUS close — uses each holding's prior close
    def _prev_close_for(ticker: str, fallback: float) -> float:
        ch = changes.get(ticker)
        if not ch:
            return fallback
        last, pct = ch
        # last / (1 + pct/100) = prev
        return last / (1 + pct / 100) if pct != -100 else fallback

    prev_value = portfolio.cash_sek + sum(
        h.shares * _prev_close_for(h.ticker, h.avg_cost) for h in portfolio.holdings.values()
    )
    portfolio_change_pct = (current_value / prev_value - 1) * 100 if prev_value else 0.0
    portfolio_change_sek = current_value - prev_value

    inception_pnl_pct = (current_value / portfolio.initial_capital_sek - 1) * 100
    cash_pct = portfolio.cash_sek / current_value * 100 if current_value else 0

    bm_str = f"{bm_change_pct:+.2f}%" if bm_change_pct is not None else "n/a"

    # Per-holding line
    held_rows = []
    for t in held_tickers:
        h = portfolio.holdings.get(t)
        if not h:
            continue
        ch = changes.get(t)
        if ch:
            price, pct = ch
        else:
            price, pct = h.avg_cost, 0.0
        total_pnl = (price / h.avg_cost - 1) * 100 if h.avg_cost else 0
        arrow = "▲" if pct > 0.01 else ("▼" if pct < -0.01 else "—")
        held_rows.append(
            f"| {t} | {price:.2f} | {arrow} {pct:+.2f}% | {total_pnl:+.2f}% |"
        )
    if held_rows:
        held_block = (
            "| Ticker | Price | Today | Total P&L |\n"
            "|---|---:|---:|---:|\n" + "\n".join(held_rows)
        )
    else:
        held_block = "_No holdings — portfolio is 100% cash._"

    # Per-watchlist line (just price + today's move)
    wl_rows = []
    for t in watchlist_tickers:
        ch = changes.get(t)
        if not ch:
            continue
        price, pct = ch
        arrow = "▲" if pct > 0.01 else ("▼" if pct < -0.01 else "—")
        wl_rows.append(f"| {t} | {price:.2f} | {arrow} {pct:+.2f}% |")
    if wl_rows:
        wl_block = (
            "| Ticker | Price | Today |\n"
            "|---|---:|---:|\n" + "\n".join(wl_rows)
        )
    else:
        wl_block = "_No watchlist tickers tracked._"

    return f"""## Where we stand

- **Portfolio**: {current_value:,.0f} SEK ({portfolio_change_pct:+.2f}% today, {portfolio_change_sek:+,.0f} SEK)
- **vs inception**: {inception_pnl_pct:+.2f}%
- **{bm_label} today**: {bm_str}
- **Cash**: {portfolio.cash_sek:,.0f} SEK ({cash_pct:.1f}%)

## Holdings today

{held_block}

## Watchlist today

{wl_block}
"""


def _build_daily_report(
    today: date,
    portfolio: Portfolio,
    prices: dict[str, float],
    changes: dict[str, tuple[float, float]],
    held_tickers: list[str],
    watchlist_tickers: list[str],
    bm_change_pct: float | None,
    bm_label: str,
    em_json: dict | None,
    em_full_text: str | None,
    monitored: list[str],
    insiders_today: list,
    movers: list[tuple[str, float]],
    todays_material_news: list,
    token_usage: dict,
    decisions_section: str = "",
) -> str:
    pulse = _pulse_section(portfolio, prices, changes, held_tickers, watchlist_tickers, bm_change_pct, bm_label)

    # News section (always shown; says "no news" on quiet days)
    if todays_material_news:
        news_rows = []
        for ticker, it in todays_material_news[:10]:
            sent_glyph = {"positive": "🟢", "neutral": "⚪", "negative": "🔴"}.get(
                it.sentiment or "neutral", "⚪"
            )
            news_rows.append(
                f"- {sent_glyph} **{ticker}** [{(it.source or '?')[:18]}] "
                f"M{it.materiality}: {it.summary or it.title}"
            )
        if len(todays_material_news) > 10:
            news_rows.append(f"- _… and {len(todays_material_news) - 10} more material items today_")
        news_section = "\n## News today (≥M3)\n\n" + "\n".join(news_rows) + "\n"
    else:
        news_section = "\n## News today\n\n_(no material news on monitored names in the last 24h)_\n"

    # Flags / AI section (only when EM was invoked)
    if em_json is not None:
        flags = em_json.get("flags", []) or []
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
            flags_block = "_(Event Monitor reviewed today's activity — no flags raised)_"

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
        em_section = f"""
## Material events

{em_json.get('summary', '(no summary)')}

### Flags ({len(em_json.get('flags', []) or [])})
{flags_block}

### Large price moves (≥ {MOVER_THRESHOLD_PCT:.0f}%)
{movers_block}

### Insider activity today
{insiders_block}

### Event Monitor reasoning
{em_full_text}

*Event Monitor cost: ${cost:.3f}.*
"""
    else:
        em_section = "\n## Material events\n\n_No material events today — Event Monitor not invoked (saves tokens)._\n"

    return f"""# Daily Pulse — {today.isoformat()}
{pulse}{news_section}{em_section}{decisions_section}
---
*Monitored: {len(monitored)} tickers ({len(held_tickers)} held + {len(watchlist_tickers)} watchlist).*
"""


def _estimate_cost(usage: dict) -> float:
    in_ = usage.get("input", 0)
    out = usage.get("output", 0)
    cr = usage.get("cache_read", 0)
    cc = usage.get("cache_create", 0)
    return (in_ * 3 + out * 15 + cr * 0.3 + cc * 3.75) / 1_000_000


def _build_subject(
    today: date,
    portfolio_change_pct: float,
    flags: list[dict],
    has_urgent: bool,
) -> str:
    arrow = "▲" if portfolio_change_pct >= 0 else "▼"
    if has_urgent:
        return f"[Investing Agent URGENT] {today.isoformat()} · {len(flags)} flag(s)"
    if flags:
        return f"[Investing Agent] {today.isoformat()} · {arrow} {portfolio_change_pct:+.2f}% · {len(flags)} flag(s)"
    return f"[Investing Agent] Daily pulse {today.isoformat()} · {arrow} {portfolio_change_pct:+.2f}%"


# --- Daily decision phase (conviction gate -> trade) ----------------------

def market_wide_candidates(
    insiders_recent: list,
    universe: list,
    monitored: list[str],
    max_candidates: int = MAX_NEW_CANDIDATES,
) -> list[str]:
    """New (not-yet-held, not-watchlist) names with significant insider buying.

    This is what lets the daily cycle open a *brand-new* position on conviction —
    the market-wide branch the weekly screener would otherwise be the only source
    of. Ranked by total insider buy value; capped to bound Analyst-call cost.
    """
    sig = filter_significant_buys(insiders_recent, min_value_sek=SIGNIFICANT_INSIDER_SEK)
    by_ticker = index_by_ticker(sig, universe)
    ranked = sorted(
        (
            (t, sum(x.total_value_sek for x in txs))
            for t, txs in by_ticker.items()
            if t not in monitored
        ),
        key=lambda kv: kv[1],
        reverse=True,
    )
    chosen = [t for t, _ in ranked[:max_candidates]]
    if len(ranked) > max_candidates:
        log.info(
            "Market-wide insider candidates: %d cleared %s SEK; deep-diving top %d (%s)",
            len(ranked), f"{SIGNIFICANT_INSIDER_SEK:,.0f}", max_candidates, chosen,
        )
    elif chosen:
        log.info("Market-wide insider candidates: %s", chosen)
    return chosen


def _analyze_candidate(
    today: date,
    ticker: str,
    universe: list,
    prices: dict[str, float],
    bd_client: BorsdataClient | None,
) -> RoleResponse | None:
    """Run one Analyst pass on a fresh buy candidate (same inputs as weekly)."""
    entry = find(ticker, universe)
    if entry is None:
        return None
    m = compute_metrics(ticker, insiders_for_ticker=[])
    if m is None:
        log.warning("No metrics for daily candidate %s — skipping analysis", ticker)
        return None

    fundamentals_block = "  (fundamentals unavailable — Börsdata not reachable or ticker not mapped)"
    insider_block = "  (no conviction-grade insider transactions in the last 90 days)"
    if bd_client is not None:
        ins_id = bd_client.yahoo_to_ins_id.get(ticker)
        if ins_id is not None:
            f = compute_fundamentals(bd_client, ins_id, current_price=prices.get(ticker))
            if f is not None:
                fundamentals_block = format_fundamentals(f)
            try:
                summaries = fetch_summaries_for_universe(bd_client, [ticker], window_days=90)
                if ticker in summaries:
                    insider_block = format_summary_for_analyst(summaries[ticker], window_days=90)
            except BorsdataError as e:
                log.warning("Börsdata insider fetch failed for %s: %s", ticker, e)

    try:
        news_items, _, _ = fetch_and_classify(entry)
        news_block = format_news_for_analyst(news_items, since_days=30, min_materiality=3)
    except Exception as e:
        log.warning("News fetch/classify failed for %s: %s", ticker, e)
        news_block = "  (news pipeline unavailable this cycle)"

    msg = analyst_user_message(
        today=today,
        entry=entry,
        angle="surfaced by a daily insider/event trigger — assess as a fresh idea",
        sleeve_hint="either",
        metrics=m,
        insider_block=insider_block,
        fundamentals_block=fundamentals_block,
        held_avg_cost=None,
        news_block=news_block,
    )
    return call_role("analyst", msg)


def _build_triggers_block(
    actionable_flags: list[dict],
    candidate_buys: list[str],
    material_news: list,
) -> str:
    parts: list[str] = []
    if actionable_flags:
        parts.append("Flags on held / watchlist names:")
        for f in actionable_flags:
            parts.append(
                f"  - [{(f.get('severity') or '').upper()}] {f.get('ticker', '?')} "
                f"({f.get('kind', '?')}): {f.get('detail', '?')}"
            )
    if candidate_buys:
        parts.append("New-name insider signals (not currently held — you may open these):")
        for t in candidate_buys:
            parts.append(f"  - {t}: significant insider buying in last 2 days — see Analyst note below")
    if material_news:
        parts.append("Material news today (>=M3):")
        for ticker, it in material_news[:8]:
            parts.append(f"  - {ticker}: M{it.materiality} {it.summary or it.title}")
    return "\n".join(parts) if parts else "(no triggers)"


def _execute_daily_trades(
    proposed_trades: list[dict],
    portfolio: Portfolio,
    prices: dict[str, float],
    sector_lookup: dict[str, str],
    currency_lookup: dict[str, str],
    dry_run: bool,
) -> tuple[list[dict], list[dict]]:
    """Validate + (unless dry-run) execute proposed trades. Mirrors the weekly tail."""
    executed: list[dict] = []
    violations: list[dict] = []
    for trade in proposed_trades:
        action_str = (trade.get("action") or "").lower()
        try:
            # "trim" is a partial sell; the risk checker only knows BUY/SELL.
            action = Action.SELL if action_str in ("sell", "trim") else Action(action_str)
            proposal = TradeProposal(
                action=action,
                ticker=trade["ticker"],
                shares=float(trade["shares"]),
                price=float(trade.get("limit_price_sek") or prices.get(trade["ticker"], 0.0)),
                sleeve=Sleeve(trade["sleeve"]),
                sector=trade.get("sector") or sector_lookup.get(trade["ticker"], "Unknown"),
                rationale=trade.get("rationale", ""),
                thesis_break=bool(trade.get("thesis_break", False)),
            )
        except (KeyError, ValueError) as e:
            violations.append({**trade, "rule": "malformed", "detail": str(e)})
            continue

        viols = check_trade(portfolio, proposal, prices=prices, sector_lookup=sector_lookup)
        if viols:
            for v in viols:
                violations.append({**trade, "rule": v.rule, "detail": v.detail})
            continue

        exec_price = prices.get(proposal.ticker, proposal.price)
        if not dry_run:
            if proposal.action == Action.BUY:
                portfolio.buy(
                    ticker=proposal.ticker, shares=proposal.shares, price=exec_price,
                    sleeve=proposal.sleeve, sector=proposal.sector, rationale=proposal.rationale,
                    currency=currency_lookup.get(proposal.ticker, "SEK"),
                )
            else:
                portfolio.sell(
                    ticker=proposal.ticker, shares=proposal.shares, price=exec_price,
                    rationale=proposal.rationale,
                )
        executed.append({**trade, "limit_price_sek": exec_price})
    return executed, violations


def _append_daily_decision_log(today: date, executed: list[dict]) -> None:
    """Append a dated line per executed daily trade to a delimited section at the
    end of theses.md, so same-day traceability holds. The weekly Journal Keeper
    folds these in and clears the section."""
    if not executed:
        return
    header = "## Daily decisions log"
    existing = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""
    lines = [
        f"- {today.isoformat()} {t['action']} {t['shares']} {t['ticker']} "
        f"@ {t['limit_price_sek']:.2f} SEK ({t['sleeve']}) — {t['rationale']}"
        for t in executed
    ]
    if header in existing:
        new_text = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    else:
        new_text = existing.rstrip() + f"\n\n{header}\n\n" + "\n".join(lines) + "\n"
    THESES_FILE.write_text(new_text, encoding="utf-8")


def _build_decisions_section(
    dpm_text: str,
    dpm_json: dict,
    executed: list[dict],
    violations: list[dict],
    dry_run: bool,
    cost: float,
) -> str:
    title = "## Daily decisions" + (" (DRY RUN — not executed)" if dry_run else "")
    lines = [f"\n{title}\n", dpm_json.get("summary", "") or ""]
    if executed:
        lines.append("\n### Executed")
        for t in executed:
            lines.append(
                f"- **{t['action']} {t['shares']} {t['ticker']}** @ {t['limit_price_sek']:.2f} SEK "
                f"({t['sleeve']}) — {t['rationale']}"
            )
    if violations:
        lines.append("\n### Blocked by risk checker")
        for v in violations:
            lines.append(f"- {v.get('action')} {v.get('shares')} {v.get('ticker')} — [{v['rule']}] {v['detail']}")
    if not executed and not violations:
        note = dpm_json.get("no_action_note") or "No action — triggers did not clear the conviction bar."
        lines.append(f"\n_{note}_")
    lines.append("\n### Daily PM reasoning\n" + dpm_text)
    lines.append(f"\n*Daily decision cost: ${cost:.3f}.*")
    return "\n".join(lines)


# --- Orchestrator ---------------------------------------------------------

def run(send_email_flag: bool, silent: bool, dry_run: bool = False) -> int:
    """`silent=True` reverts to old behaviour: only email when flags exist.

    `dry_run=True` runs the full decision path but does NOT mutate portfolio.json
    or theses.md — for safe testing. Default is live (the daily cycle trades)."""
    _setup_logging()
    today = date.today()
    log.info("=== Daily cycle start — %s ===", today.isoformat())

    portfolio = Portfolio.load()
    bd_client: BorsdataClient | None = None
    try:
        bd_client = BorsdataClient()
    except BorsdataError as e:
        log.warning("Börsdata client init failed: %s", e)
    universe = merged_universe(bd_client)
    journal = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""

    watchlist_tickers = parse_watchlist(journal)
    held_tickers = list(portfolio.holdings.keys())
    monitored = list(dict.fromkeys(held_tickers + watchlist_tickers))
    log.info(
        "Monitoring %d tickers (%d held + %d watchlist)",
        len(monitored), len(held_tickers), len(watchlist_tickers),
    )

    # Prices for monitored tickers (latest closes, SEK-normalised)
    prices = get_latest_closes_sek(monitored) if monitored else {}

    # Per-ticker daily price changes (single yfinance call per ticker, cheap)
    changes = daily_changes(monitored) if monitored else {}

    # Blended OMXS30 + S&P 500 daily change for the pulse (weighted by the
    # portfolio's current regional equity split; S&P measured in SEK).
    se_w, us_w = regional_weights(portfolio, prices)
    bm_label, bm_change_pct = blended_daily_change(se_w, us_w)

    # Insider activity (last 2 days). Fetched unconditionally: the monitored
    # subset feeds the Event Monitor, while the full set powers the market-wide
    # candidate scan that lets the daily cycle open brand-new positions.
    log.info("Fetching FI insider transactions (last 2 days) ...")
    insiders_recent = fetch_recent(days_back=2)
    insiders_by_ticker = index_by_ticker(insiders_recent, universe)
    insiders_today: list = []
    for t in monitored:
        insiders_today.extend(insiders_by_ticker.get(t, []))
    log.info("Found %d insider transactions on monitored names", len(insiders_today))

    movers = compute_daily_movers(changes)

    # News: fetch + classify for all monitored tickers
    monitored_entries = [e for t in monitored for e in [find(t, universe)] if e is not None]
    news_by_ticker = {}
    if monitored_entries:
        try:
            news_by_ticker = fetch_and_classify_many(monitored_entries, max_new_per_ticker=4)
        except Exception as e:
            log.warning("News fetch failed (non-fatal): %s", e)

    todays_material_news = recent_high_materiality(news_by_ticker, since_days=1, min_materiality=3)
    log.info("Today's material news items (≥M3, last 24h): %d", len(todays_material_news))

    has_material = bool(insiders_today) or bool(movers) or bool(todays_material_news)
    log.info(
        "Daily inputs: %d insider txs, %d large movers, %d material news. Material=%s",
        len(insiders_today), len(movers), len(todays_material_news), has_material,
    )

    # Invoke Event Monitor only on material days. The daily pulse below
    # is built either way.
    em_json: dict | None = None
    em_text: str | None = None
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    if has_material:
        # Build a news block for the Event Monitor (last 24h, ≥M3)
        if todays_material_news:
            news_block_lines = [
                f"  [{ticker}] {it.published_at.date().isoformat()} "
                f"({(it.source or '?')[:18]}) M{it.materiality}{('+' if it.sentiment=='positive' else '-' if it.sentiment=='negative' else '·')} "
                f"{it.summary or it.title}"
                for ticker, it in todays_material_news[:12]
            ]
            em_news_block = "\n".join(news_block_lines)
        else:
            em_news_block = "  (no notable news in the last 24 hours)"

        msg = event_monitor_user_message(
            today=today, portfolio=portfolio, prices=prices, journal=journal,
            insiders_today=insiders_today, large_movers=movers,
            news_block=em_news_block,
        )
        resp = call_role("event_monitor", msg)
        usage = {
            "input": resp.input_tokens, "output": resp.output_tokens,
            "cache_read": resp.cache_read_tokens, "cache_create": resp.cache_creation_tokens,
        }
        em_text = resp.text
        try:
            em_json = extract_json(resp.text)
        except JsonExtractError as e:
            log.warning("Event Monitor JSON parse failed: %s", e)
            em_json = {"summary": "Event Monitor JSON parse failed.", "flags": []}
    else:
        log.info("No material inputs — skipping Event Monitor Claude call.")

    # --- Daily decision phase (conviction gate -> trade) ---
    # Escalate to the Daily PM (Opus) only when something material surfaced:
    # an actionable flag on a held/watchlist name, or a fresh insider signal on
    # a new name. Quiet days make zero Opus calls — no-action is the default.
    executed_trades: list[dict] = []
    risk_violations: list[dict] = []
    decisions_section = ""
    sector_lookup = {e.ticker: e.sector for e in universe}
    currency_lookup = {e.ticker: e.currency for e in universe}

    em_flags = (em_json or {}).get("flags", []) or []
    actionable_flags = [
        f for f in em_flags if (f.get("severity") or "").lower() in ("watch", "urgent")
    ]
    candidate_buys = market_wide_candidates(insiders_recent, universe, monitored)

    if actionable_flags or candidate_buys:
        log.info(
            "Escalating to daily_pm: %d actionable flag(s), %d new-name candidate(s)",
            len(actionable_flags), len(candidate_buys),
        )
        decision_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}

        # Price any candidate names we haven't already priced (they're new).
        unpriced = [t for t in candidate_buys if t not in prices]
        for t, px in (get_latest_closes_sek(unpriced) if unpriced else {}).items():
            prices[t] = px

        # Fresh Analyst pass on each new-name buy candidate.
        analyst_texts: list[str] = []
        for t in candidate_buys:
            resp = _analyze_candidate(today, t, universe, prices, bd_client)
            if resp is not None:
                analyst_texts.append(resp.text)
                decision_usage["input"] += resp.input_tokens
                decision_usage["output"] += resp.output_tokens
                decision_usage["cache_read"] += resp.cache_read_tokens
                decision_usage["cache_create"] += resp.cache_creation_tokens

        triggers_block = _build_triggers_block(actionable_flags, candidate_buys, todays_material_news)
        us_involved = any(currency_lookup.get(t) == "USD" for t in candidate_buys) or any(
            (h.currency or "SEK").upper() == "USD" for h in portfolio.holdings.values()
        )
        fx_note = ""
        if us_involved:
            from src.data import fx
            fx_note = (
                f"1 USD = {fx.rate('USD'):.2f} SEK today. US prices/values shown are SEK-normalised; "
                f"limit_price_sek should be the SEK-equivalent."
            )
        dpm_msg = daily_pm_user_message(
            today=today, portfolio=portfolio, prices=prices, journal=journal,
            triggers_block=triggers_block, analyst_full_text=analyst_texts, fx_note=fx_note,
        )
        dpm_resp = call_role("daily_pm", dpm_msg)
        decision_usage["input"] += dpm_resp.input_tokens
        decision_usage["output"] += dpm_resp.output_tokens
        decision_usage["cache_read"] += dpm_resp.cache_read_tokens
        decision_usage["cache_create"] += dpm_resp.cache_creation_tokens

        try:
            dpm_json = extract_json(dpm_resp.text)
        except JsonExtractError as e:
            log.warning("Daily PM JSON parse failed: %s", e)
            dpm_json = {"summary": "Daily PM JSON parse failed.", "trades": []}

        proposed = dpm_json.get("trades", []) or []
        log.info("Daily PM proposed %d trade(s)", len(proposed))
        executed_trades, risk_violations = _execute_daily_trades(
            proposed, portfolio, prices, sector_lookup, currency_lookup, dry_run,
        )

        if not dry_run and executed_trades:
            portfolio.save()
            _append_daily_decision_log(today, executed_trades)
            log.info("Executed %d daily trade(s); portfolio.json + theses.md updated.", len(executed_trades))
        elif dry_run:
            log.info("Dry run — %d trade(s) would execute; state NOT saved.", len(executed_trades))

        decisions_section = _build_decisions_section(
            dpm_resp.text, dpm_json, executed_trades, risk_violations, dry_run,
            _estimate_cost(decision_usage),
        )
    else:
        log.info("No actionable triggers — no Daily PM call (no-action is the default).")

    # Build the daily pulse report (always)
    report_md = _build_daily_report(
        today=today, portfolio=portfolio, prices=prices, changes=changes,
        held_tickers=held_tickers, watchlist_tickers=watchlist_tickers,
        bm_change_pct=bm_change_pct, bm_label=bm_label,
        em_json=em_json, em_full_text=em_text,
        monitored=monitored, insiders_today=insiders_today, movers=movers,
        todays_material_news=todays_material_news,
        token_usage=usage,
        decisions_section=decisions_section,
    )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"daily_{today.isoformat()}.md"
    report_path.write_text(report_md, encoding="utf-8")
    log.info("Report written to %s", report_path)

    # Email decision
    flags = (em_json or {}).get("flags", []) or []
    has_urgent = any((f.get("severity") or "").lower() == "urgent" for f in flags)

    # Portfolio change for subject (recompute from pulse logic)
    current_value = portfolio.cash_sek + sum(
        h.shares * prices.get(h.ticker, h.avg_cost) for h in portfolio.holdings.values()
    )
    prev_value = portfolio.cash_sek + sum(
        h.shares * (changes.get(h.ticker, (h.avg_cost, 0.0))[0] / (1 + changes.get(h.ticker, (0, 0.0))[1] / 100)
                    if h.ticker in changes else h.avg_cost)
        for h in portfolio.holdings.values()
    )
    pct_today = (current_value / prev_value - 1) * 100 if prev_value else 0.0

    # In silent mode, only email when there are flags OR executed trades.
    # Default mode: always email the daily pulse.
    should_email = send_email_flag and (not silent or flags or executed_trades)
    if should_email:
        subject = _build_subject(today, pct_today, flags, has_urgent)
        if executed_trades:
            tag = "DRY-RUN TRADE" if dry_run else "TRADED"
            subject = f"[Investing Agent {tag}] {today.isoformat()} · {len(executed_trades)} trade(s)"
        send_email(subject=subject, body_markdown=report_md)
    else:
        log.info("Email skipped (silent mode without flags, or --no-email).")

    # Regenerate dashboard with fresh prices — pure Python, no AI cost
    try:
        from src.dashboard import build_and_write_dashboard
        build_and_write_dashboard()
        log.info("Dashboard regenerated.")
    except Exception as e:
        log.warning("Dashboard regen failed (non-fatal): %s", e)

    log.info("=== Daily cycle complete ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily pulse + event-monitor cycle")
    parser.add_argument(
        "--silent", action="store_true",
        help="Old behaviour: only email when flags exist (default = always email the daily pulse).",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Never email (just build the report file + dashboard).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the full decision path but do NOT mutate portfolio.json / theses.md.",
    )
    args = parser.parse_args()
    return run(send_email_flag=not args.no_email, silent=args.silent, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
