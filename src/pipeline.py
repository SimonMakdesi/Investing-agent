"""Unified decision pipeline (v2: Scout -> Analyst -> Trader -> Journal).

One orchestrator runs both cadences:
  - daily  (Mon-Fri 22:00 UTC): reactive. Scout scans the whole universe + book,
    Analyst deep-dives up to MAX_ANALYST_PER_DAY surfaced names, Trader decides,
    decisions are appended to the journal's daily log.
  - deep   (Saturday): proactive. Same ladder, but the Trader re-examines EVERY
    holding for rotation and the Journal Keeper rewrites theses.md from scratch.

Run modes:
    uv run python -m src.pipeline            # daily, live
    uv run python -m src.pipeline --deep     # weekly deep review, live
    uv run python -m src.pipeline --dry-run  # no state mutation
"""

from __future__ import annotations

import argparse
import copy
import logging
import re
import sys
from datetime import date

from src.claude_client import call_role
from src.config import MIN_AVG_TURNOVER, MONTHLY_CONTRIBUTION_SEK, REPORTS_DIR, THESES_FILE
from src.contexts import (
    analyst_user_message,
    journal_keeper_user_message,
    scout_user_message,
    trader_user_message,
)
from src.dashboard import build_and_write_dashboard
from src.data.borsdata import BorsdataClient, BorsdataError
from src.data.borsdata_insiders import fetch_summaries_for_universe, format_summary_for_analyst
from src.data.fundamentals import compute as compute_fundamentals
from src.data.fundamentals import format_for_analyst as format_fundamentals
from src.data.insiders import fetch_recent
from src.data.news import fetch_and_classify, format_for_analyst as format_news_for_analyst
from src.data.prices import get_latest_closes_sek
from src.data.universe_refresh import merged_universe
from src.issuer_match import index_by_ticker
from src.json_parse import JsonExtractError, extract_json
from src.metrics import compute as compute_metrics
from src.pace import compute_pace
from src.portfolio import Portfolio, Sleeve
from src.reporting import build_report, send_email
from src.risk import Action, TradeProposal, check_trade
from src.universe import find

log = logging.getLogger(__name__)

# Conviction-gated ceiling on deep Analyst calls per cycle (CLAUDE.md §, owner-set).
MAX_ANALYST_PER_DAY = 8


def _setup_logging() -> None:
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


def _accumulate(usage: dict, resp) -> None:
    usage["input"] = usage.get("input", 0) + resp.input_tokens
    usage["output"] = usage.get("output", 0) + resp.output_tokens
    usage["cache_read"] = usage.get("cache_read", 0) + resp.cache_read_tokens
    usage["cache_create"] = usage.get("cache_create", 0) + resp.cache_creation_tokens


def parse_watchlist(journal_text: str) -> list[str]:
    """Pull tickers from the Watchlist section of the journal (SE + US)."""
    if not journal_text:
        return []
    m = re.search(
        r"^##\s*\d*\.?\s*Watchlist\s*\n(.*?)(?=^##\s|\Z)",
        journal_text, re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    if not m:
        return []
    section = m.group(1)
    # Swedish .ST tickers and bare US tickers in bold (**AAPL**) or backticks.
    tickers = re.findall(r"\b[A-Z][A-Z0-9-]*\.ST\b", section)
    tickers += re.findall(r"\*\*([A-Z]{1,5})\*\*", section)
    return list(dict.fromkeys(tickers))


def is_contribution_due(portfolio: Portfolio, today: date) -> bool:
    """True if no external top-up has been recorded in the current calendar month.

    Gives exactly one MONTHLY_CONTRIBUTION_SEK injection per month, on the first
    run of the month, regardless of how many times the pipeline runs."""
    for c in portfolio.contributions:
        if c.date.year == today.year and c.date.month == today.month:
            return False
    return True


def _analyst_pass(today, entry, angle, metrics, prices, borsdata, held_avg_cost):
    """Run one Analyst call on a name (shared by daily and deep)."""
    fundamentals_block = "  (fundamentals unavailable — Börsdata not reachable or ticker not mapped)"
    insider_block = "  (no conviction-grade insider transactions in the last 90 days)"
    if borsdata is not None:
        ins_id = borsdata.yahoo_to_ins_id.get(entry.ticker)
        if ins_id is not None:
            f = compute_fundamentals(borsdata, ins_id, current_price=prices.get(entry.ticker))
            if f is not None:
                fundamentals_block = format_fundamentals(f)
            try:
                summaries = fetch_summaries_for_universe(borsdata, [entry.ticker], window_days=90)
                if entry.ticker in summaries:
                    insider_block = format_summary_for_analyst(summaries[entry.ticker], window_days=90)
            except BorsdataError as e:
                log.warning("Börsdata insider fetch failed for %s: %s", entry.ticker, e)

    try:
        news_items, _, _ = fetch_and_classify(entry)
        news_block = format_news_for_analyst(news_items, since_days=30, min_materiality=3)
    except Exception as e:
        log.warning("News fetch/classify failed for %s: %s", entry.ticker, e)
        news_block = "  (news pipeline unavailable this cycle)"

    msg = analyst_user_message(
        today=today, entry=entry, angle=angle, metrics=metrics,
        insider_block=insider_block, fundamentals_block=fundamentals_block,
        held_avg_cost=held_avg_cost, news_block=news_block,
    )
    return call_role("analyst", msg)


def run(*, deep: bool, dry_run: bool, send_email_flag: bool) -> int:
    _setup_logging()
    today = date.today()
    mode = "DEEP weekly" if deep else "DAILY"
    log.info("=== %s cycle start — %s — dry_run=%s ===", mode, today.isoformat(), dry_run)

    portfolio = Portfolio.load()
    portfolio_before = copy.deepcopy(portfolio)

    borsdata: BorsdataClient | None = None
    try:
        borsdata = BorsdataClient()
    except BorsdataError as e:
        log.warning("Börsdata client init failed: %s", e)
    universe = merged_universe(borsdata)
    journal = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""
    watchlist = parse_watchlist(journal)

    # --- Prices: only what we VALUE and TRADE (held + watchlist now; candidates
    # after the Scout). The universe's prices already live in the metrics, so a
    # full-universe latest-close fetch is both redundant and a yfinance-throttle
    # trigger (it was returning NaN closes). Keeps the daily run lean. ---
    held_tickers = list(portfolio.holdings.keys())
    price_tickers = sorted(set(held_tickers + watchlist))
    log.info("Fetching prices for %d held+watchlist tickers ...", len(price_tickers))
    prices = get_latest_closes_sek(price_tickers)
    log.info("Got prices for %d/%d tickers", len(prices), len(price_tickers))

    # --- Monthly external top-up (a contribution, NOT a gain) ---
    # Added before the Scout runs so the fresh cash is deployable same-cycle.
    contributed_sek = 0.0
    if is_contribution_due(portfolio, today):
        value_before = portfolio.value(prices)
        # Apply in-memory either way (so dry-run previews reflect the new cash);
        # only persist the transaction-log entry on a live run.
        portfolio.contribute(MONTHLY_CONTRIBUTION_SEK, value_before, log_txn=not dry_run)
        contributed_sek = MONTHLY_CONTRIBUTION_SEK
        log.info("Monthly contribution %s: +%s SEK (value before: %s SEK)",
                 "(dry-run, not saved)" if dry_run else "added",
                 f"{MONTHLY_CONTRIBUTION_SEK:,.0f}", f"{value_before:,.0f}")

    # --- Insider data (FI) ---
    insiders_recent = fetch_recent(days_back=7)
    insiders_by_ticker_90d = index_by_ticker(fetch_recent(days_back=90), universe)

    # --- Universe metrics, with liquidity floor (held names always kept) ---
    metrics_by_ticker: dict = {}
    dropped_illiquid = 0
    for entry in universe:
        m = compute_metrics(entry.ticker, insiders_for_ticker=insiders_by_ticker_90d.get(entry.ticker, []))
        if m is None:
            continue
        if m.avg_turnover_30d < MIN_AVG_TURNOVER and entry.ticker not in portfolio.holdings:
            dropped_illiquid += 1
            continue
        metrics_by_ticker[entry.ticker] = m
    log.info("Metrics: %d names kept, %d dropped below liquidity floor (%.0fM)",
             len(metrics_by_ticker), dropped_illiquid, MIN_AVG_TURNOVER / 1e6)

    # --- Pace ---
    current_value = portfolio.value(prices)
    pace = compute_pace(portfolio, current_value, today)
    log.info("Pace: %s", pace.one_liner())

    usage_total: dict = {}

    # --- Scout ---
    log.info("--- Scout ---")
    scout_msg = scout_user_message(
        today=today, deep=deep, portfolio=portfolio, prices=prices,
        metrics=list(metrics_by_ticker.values()), insiders_recent=insiders_recent,
        watchlist=watchlist, pace=pace,
    )
    scout_resp = call_role("scout", scout_msg)
    _accumulate(usage_total, scout_resp)
    try:
        scout_json = extract_json(scout_resp.text)
    except JsonExtractError as e:
        log.error("Scout returned unparseable JSON: %s", e)
        scout_json = {"buy_candidates": [], "sell_candidates": []}

    buy_candidates = (scout_json.get("buy_candidates", []) or [])[:MAX_ANALYST_PER_DAY]
    sell_candidates = scout_json.get("sell_candidates", []) or []
    log.info("Scout: %d buy candidate(s) (cap %d), %d sell flag(s)",
             len(buy_candidates), MAX_ANALYST_PER_DAY, len(sell_candidates))

    # Price the surfaced candidates now (they weren't in the held+watchlist set).
    cand_tickers = [p.get("ticker") for p in buy_candidates if p.get("ticker") and p.get("ticker") not in prices]
    if cand_tickers:
        for t, px in get_latest_closes_sek(cand_tickers).items():
            prices[t] = px
        log.info("Priced %d Scout candidate(s)", len(cand_tickers))

    # --- Analyst (one call per buy candidate, capped) ---
    analyst_full_text: list[str] = []
    analyst_parsed: list[dict] = []
    for pick in buy_candidates:
        ticker = pick.get("ticker")
        entry = find(ticker, universe)
        if entry is None:
            log.warning("Scout surfaced unknown ticker %s — skipping", ticker)
            continue
        m = metrics_by_ticker.get(ticker)
        if m is None:
            log.warning("No metrics for %s — skipping analysis", ticker)
            continue
        held = portfolio.holdings.get(ticker)
        resp = _analyst_pass(
            today, entry, pick.get("angle", ""), m, prices, borsdata,
            held.avg_cost if held else None,
        )
        _accumulate(usage_total, resp)
        analyst_full_text.append(resp.text)
        try:
            analyst_parsed.append(extract_json(resp.text))
        except JsonExtractError as e:
            log.warning("Analyst note on %s missing parseable JSON: %s", ticker, e)
            analyst_parsed.append({"ticker": ticker, "verdict": "PARSE_ERROR", "error": str(e)})

    # --- Trader (only when there's something to decide) ---
    trader_text = ""
    executed_trades: list[dict] = []
    risk_violations: list[dict] = []
    sector_lookup = {e.ticker: e.sector for e in universe}
    currency_lookup = {e.ticker: e.currency for e in universe}

    should_trade = bool(analyst_parsed) or bool(sell_candidates) or deep
    if should_trade:
        log.info("--- Trader ---")
        us_involved = any(currency_lookup.get(p.get("ticker")) == "USD" for p in buy_candidates) or any(
            (h.currency or "SEK").upper() == "USD" for h in portfolio.holdings.values()
        )
        fx_note = ""
        if us_involved:
            from src.data import fx
            fx_note = (
                f"1 USD = {fx.rate('USD'):.2f} SEK today. US prices/values are SEK-normalised; "
                f"limit_price_sek should be the SEK-equivalent."
            )
        trader_msg = trader_user_message(
            today=today, deep=deep, portfolio=portfolio, prices=prices,
            analyst_notes=analyst_parsed, analyst_full_text=analyst_full_text,
            sell_candidates=sell_candidates, pace=pace, fx_note=fx_note,
        )
        trader_resp = call_role("trader", trader_msg)
        _accumulate(usage_total, trader_resp)
        trader_text = trader_resp.text
        try:
            trader_json = extract_json(trader_resp.text)
        except JsonExtractError as e:
            log.error("Trader returned unparseable JSON: %s — no trades this cycle.", e)
            trader_json = {"summary": "Trader JSON parse failed.", "trades": []}

        proposed = trader_json.get("trades", []) or []
        log.info("Trader proposed %d trade(s)", len(proposed))
        executed_trades, risk_violations = _execute(
            proposed, portfolio, prices, sector_lookup, currency_lookup, dry_run,
        )
    else:
        log.info("Quiet cycle — no candidates and no sell flags; Trader not called.")

    # --- Persist (trades and/or the monthly contribution) ---
    if not dry_run and (executed_trades or contributed_sek):
        portfolio.save()
        if executed_trades and not deep:
            _append_daily_log(today, executed_trades)
        log.info("Saved portfolio.json (%d trade(s); contribution=%s)",
                 len(executed_trades), f"{contributed_sek:,.0f}" if contributed_sek else "none")

    # --- Journal Keeper (deep only) ---
    new_journal = journal
    if deep:
        log.info("--- Journal Keeper ---")
        jk_msg = journal_keeper_user_message(
            today=today, previous_journal=journal, scout_text=scout_resp.text,
            analyst_full_text=analyst_full_text, trader_full_text=trader_text,
            executed_trades=executed_trades, risk_violations=risk_violations,
            portfolio_after=portfolio, prices=prices, pace=pace,
        )
        jk_resp = call_role("journal_keeper", jk_msg)
        _accumulate(usage_total, jk_resp)
        new_journal = jk_resp.text.strip()
        if not dry_run:
            THESES_FILE.write_text(new_journal, encoding="utf-8")
            log.info("theses.md rewritten by Journal Keeper")

    # --- Report + deliver ---
    report_md = build_report(
        today=today, deep=deep, portfolio_before=portfolio_before, portfolio_after=portfolio,
        prices=prices, pace=pace, executed_trades=executed_trades, risk_violations=risk_violations,
        trader_summary=trader_text, journal_text=new_journal if deep else "",
        dry_run=dry_run, token_usage=usage_total,
        contributed_this_cycle=contributed_sek,
        total_contributed=portfolio.total_contributed(),
        invested_gain_sek=portfolio.invested_gain_sek(prices),
    )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = "deep" if deep else "daily"
    suffix = "-dryrun" if dry_run else ""
    report_path = REPORTS_DIR / f"{tag}_{today.isoformat()}{suffix}.md"
    report_path.write_text(report_md, encoding="utf-8")
    log.info("Report written to %s", report_path)

    if send_email_flag:
        n = len(executed_trades)
        if n:
            verb = "DRY-RUN TRADE" if dry_run else "TRADED"
            subject = f"[Investing Agent {verb}] {today.isoformat()} · {n} trade(s)"
        else:
            subject = f"[Investing Agent] {mode} pulse {today.isoformat()} · {pace.actual_return_pct:+.1f}%"
        send_email(subject=subject, body_markdown=report_md)

    try:
        build_and_write_dashboard()
    except Exception as e:
        log.warning("Dashboard generation failed: %s (non-fatal)", e)

    log.info("=== %s cycle complete ===", mode)
    return 0


def _execute(proposed, portfolio, prices, sector_lookup, currency_lookup, dry_run):
    """Validate + (unless dry-run) execute trades. sell-before-buy ordering is the
    Trader's responsibility; we execute in the given order."""
    executed: list[dict] = []
    violations: list[dict] = []
    for trade in proposed:
        action_str = (trade.get("action") or "").lower()
        try:
            action = Action.SELL if action_str in ("sell", "trim") else Action(action_str)
            proposal = TradeProposal(
                action=action,
                ticker=trade["ticker"],
                shares=float(trade["shares"]),
                price=float(trade.get("limit_price_sek") or prices.get(trade["ticker"], 0.0)),
                sector=trade.get("sector") or sector_lookup.get(trade["ticker"], "Unknown"),
                rationale=trade.get("rationale", ""),
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
                    sleeve=Sleeve.CORE, sector=proposal.sector, rationale=proposal.rationale,
                    currency=currency_lookup.get(proposal.ticker, "SEK"),
                )
            else:
                portfolio.sell(
                    ticker=proposal.ticker, shares=proposal.shares, price=exec_price,
                    rationale=proposal.rationale,
                )
        executed.append({**trade, "limit_price_sek": exec_price})
    return executed, violations


def _append_daily_log(today: date, executed: list[dict]) -> None:
    """Append a dated line per executed daily trade to the journal's daily log."""
    if not executed:
        return
    header = "## Daily decisions log"
    existing = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""
    lines = [
        f"- {today.isoformat()} {t['action']} {t['shares']} {t['ticker']} "
        f"@ {t['limit_price_sek']:.2f} SEK — {t['rationale']}"
        for t in executed
    ]
    if header in existing:
        new_text = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    else:
        new_text = existing.rstrip() + f"\n\n{header}\n\n" + "\n".join(lines) + "\n"
    THESES_FILE.write_text(new_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Investing agent decision cycle (v2)")
    parser.add_argument("--deep", action="store_true", help="Weekly deep review (full rotation + journal rewrite).")
    parser.add_argument("--dry-run", action="store_true", help="Do not mutate portfolio.json or theses.md.")
    parser.add_argument("--no-email", action="store_true", help="Skip email (still writes report file).")
    args = parser.parse_args()
    return run(deep=args.deep, dry_run=args.dry_run, send_email_flag=not args.no_email)


if __name__ == "__main__":
    sys.exit(main())
