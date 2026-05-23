"""Weekly cycle orchestrator.

Pipeline: refresh data → screen → analyze → decide → validate → (execute) → journal → report → email.

Run modes:
    uv run python -m src.pipeline_weekly --dry-run     # safe — no portfolio changes, no journal overwrite
    uv run python -m src.pipeline_weekly --live        # real — mutates state/portfolio.json and theses.md
    uv run python -m src.pipeline_weekly --dry-run --no-email   # quiet dry run

The dry-run mode writes a report file to reports/ and (by default) emails it,
but does not touch portfolio.json, theses.md, or transactions.log.
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from src.claude_client import call_role
from src.config import REPORTS_DIR, STOCKHOLM_TZ, THESES_FILE
from src.contexts import (
    analyst_user_message,
    journal_keeper_user_message,
    portfolio_manager_user_message,
    screener_user_message,
)
from src.data.insiders import InsiderTransaction, fetch_recent
from src.data.prices import get_latest_closes
from src.json_parse import JsonExtractError, extract_json
from src.metrics import compute, index_insiders_by_issuer
from src.portfolio import Portfolio, Sleeve
from src.reporting import build_weekly_report, send_email
from src.risk import Action, TradeProposal, check_trade
from src.universe import UniverseEntry, find, load_universe

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def _accumulate(usage: dict, response_in: int, response_out: int, cache_read: int, cache_create: int) -> None:
    usage["input"] = usage.get("input", 0) + response_in
    usage["output"] = usage.get("output", 0) + response_out
    usage["cache_read"] = usage.get("cache_read", 0) + cache_read
    usage["cache_create"] = usage.get("cache_create", 0) + cache_create


def _match_insiders_to_ticker(
    entry: UniverseEntry,
    by_issuer: dict[str, list[InsiderTransaction]],
) -> list[InsiderTransaction]:
    """Best-effort name match between universe entry and FI issuer names."""
    name_lower = entry.name.lower()
    short = name_lower.split()[0]
    matches: list[InsiderTransaction] = []
    for issuer, txs in by_issuer.items():
        iss = issuer.lower()
        if name_lower in iss or short in iss or iss.startswith(short):
            matches.extend(txs)
    return matches


def run(dry_run: bool, send_email_flag: bool) -> int:
    _setup_logging()
    today = date.today()
    log.info("=== Weekly cycle start — %s — dry_run=%s ===", today.isoformat(), dry_run)

    # --- Load state ---
    portfolio = Portfolio.load()
    portfolio_before = copy.deepcopy(portfolio)

    universe = load_universe()
    log.info("Loaded portfolio (cash=%,.0f, holdings=%d) and universe (%d names)",
             portfolio.cash_sek, len(portfolio.holdings), len(universe))

    # --- Data refresh ---
    log.info("Fetching prices for %d universe + %d held tickers ...",
             len(universe), len(portfolio.holdings))
    universe_tickers = [e.ticker for e in universe]
    held_tickers = list(portfolio.holdings.keys())
    all_tickers = sorted(set(universe_tickers + held_tickers))
    snapshots = get_latest_closes(all_tickers)
    prices = {t: s.close for t, s in snapshots.items()}
    log.info("Got prices for %d/%d tickers", len(prices), len(all_tickers))

    log.info("Fetching insider transactions (last 7 days + last 90 for analyst lookup)")
    insiders_7d = fetch_recent(days_back=7)
    insiders_90d = fetch_recent(days_back=90)
    by_issuer_90d = index_insiders_by_issuer(insiders_90d)

    # --- Universe metrics ---
    log.info("Computing metrics for %d tickers ...", len(universe))
    metrics_by_ticker: dict[str, "compute.__class__"] = {}
    for entry in universe:
        m = compute(entry.ticker, insiders_for_ticker=by_issuer_90d.get(entry.name, []))
        if m is not None:
            metrics_by_ticker[entry.ticker] = m
    log.info("Built metrics for %d tickers", len(metrics_by_ticker))

    usage_total: dict = {}

    # --- Screener ---
    log.info("--- Screener ---")
    screener_msg = screener_user_message(
        today=today,
        portfolio=portfolio,
        prices=prices,
        metrics=list(metrics_by_ticker.values()),
        insiders_7d=insiders_7d,
    )
    screener_resp = call_role("screener", screener_msg)
    _accumulate(usage_total, screener_resp.input_tokens, screener_resp.output_tokens,
                screener_resp.cache_read_tokens, screener_resp.cache_creation_tokens)
    try:
        screener_json = extract_json(screener_resp.text)
    except JsonExtractError as e:
        log.error("Screener returned unparseable JSON: %s", e)
        return _fail_out("Screener output could not be parsed.", today, dry_run, send_email_flag)
    picks = screener_json.get("picks", []) or []
    log.info("Screener picks: %s", [p.get("ticker") for p in picks])

    # --- Analyst (one call per pick) ---
    log.info("--- Analyst (%d picks) ---", len(picks))
    analyst_full_text: list[str] = []
    analyst_parsed: list[dict] = []
    for pick in picks:
        ticker = pick.get("ticker")
        entry = find(ticker, universe)
        if entry is None:
            log.warning("Screener picked unknown ticker %s — skipping", ticker)
            continue
        m = metrics_by_ticker.get(ticker)
        if m is None:
            log.warning("No metrics for picked ticker %s — skipping", ticker)
            continue
        held = portfolio.holdings.get(ticker)
        msg = analyst_user_message(
            today=today,
            entry=entry,
            angle=pick.get("angle", ""),
            sleeve_hint=pick.get("sleeve_hint", "either"),
            metrics=m,
            insiders_for_ticker=by_issuer_90d.get(entry.name, []),
            held_avg_cost=held.avg_cost if held else None,
        )
        resp = call_role("analyst", msg)
        _accumulate(usage_total, resp.input_tokens, resp.output_tokens,
                    resp.cache_read_tokens, resp.cache_creation_tokens)
        analyst_full_text.append(resp.text)
        try:
            analyst_parsed.append(extract_json(resp.text))
        except JsonExtractError as e:
            log.warning("Analyst note on %s missing parseable JSON: %s", ticker, e)
            analyst_parsed.append({"ticker": ticker, "verdict": "PARSE_ERROR", "error": str(e)})

    # --- Portfolio Manager ---
    log.info("--- Portfolio Manager ---")
    pm_msg = portfolio_manager_user_message(
        today=today,
        portfolio=portfolio,
        prices=prices,
        analyst_notes=analyst_parsed,
        analyst_full_text=analyst_full_text,
    )
    pm_resp = call_role("portfolio_manager", pm_msg)
    _accumulate(usage_total, pm_resp.input_tokens, pm_resp.output_tokens,
                pm_resp.cache_read_tokens, pm_resp.cache_creation_tokens)
    try:
        pm_json = extract_json(pm_resp.text)
    except JsonExtractError as e:
        log.error("PM returned unparseable JSON: %s — aborting trades for this cycle.", e)
        pm_json = {"summary": "PM JSON parse failed.", "trades": []}

    proposed_trades = pm_json.get("trades", []) or []
    log.info("PM proposed %d trades", len(proposed_trades))

    # --- Risk validation + (optional) execution ---
    sector_lookup = {e.ticker: e.sector for e in universe}
    executed_trades: list[dict] = []
    risk_violations: list[dict] = []
    for trade in proposed_trades:
        try:
            proposal = TradeProposal(
                action=Action(trade["action"]),
                ticker=trade["ticker"],
                shares=float(trade["shares"]),
                price=float(trade.get("limit_price_sek") or prices.get(trade["ticker"], 0.0)),
                sleeve=Sleeve(trade["sleeve"]),
                sector=trade.get("sector") or sector_lookup.get(trade["ticker"], "Unknown"),
                rationale=trade.get("rationale", ""),
            )
        except (KeyError, ValueError) as e:
            risk_violations.append({**trade, "rule": "malformed", "detail": str(e)})
            continue

        violations = check_trade(portfolio, proposal, prices=prices, sector_lookup=sector_lookup)
        if violations:
            for v in violations:
                risk_violations.append({
                    **trade,
                    "rule": v.rule,
                    "detail": v.detail,
                })
            continue

        # Execute (or simulate)
        exec_price = prices.get(proposal.ticker, proposal.price)
        if not dry_run:
            if proposal.action == Action.BUY:
                portfolio.buy(
                    ticker=proposal.ticker, shares=proposal.shares, price=exec_price,
                    sleeve=proposal.sleeve, sector=proposal.sector, rationale=proposal.rationale,
                )
            else:  # sell or trim
                portfolio.sell(
                    ticker=proposal.ticker, shares=proposal.shares, price=exec_price,
                    rationale=proposal.rationale,
                )
        executed_trades.append({**trade, "limit_price_sek": exec_price})

    # --- Journal Keeper ---
    log.info("--- Journal Keeper ---")
    previous_journal = THESES_FILE.read_text(encoding="utf-8") if THESES_FILE.exists() else ""
    journal_msg = journal_keeper_user_message(
        today=today,
        previous_journal=previous_journal,
        screener_text=screener_resp.text,
        analyst_full_text=analyst_full_text,
        pm_full_text=pm_resp.text,
        executed_trades=executed_trades,
        risk_violations=risk_violations,
        portfolio_after=portfolio,
        prices=prices,
    )
    journal_resp = call_role("journal_keeper", journal_msg)
    _accumulate(usage_total, journal_resp.input_tokens, journal_resp.output_tokens,
                journal_resp.cache_read_tokens, journal_resp.cache_creation_tokens)
    new_journal = journal_resp.text.strip()

    # --- Persist state ---
    if not dry_run:
        portfolio.save()
        THESES_FILE.write_text(new_journal, encoding="utf-8")
        log.info("State saved: portfolio.json, theses.md")
    else:
        log.info("Dry run — state NOT saved.")

    # --- Build and deliver report ---
    report_md = build_weekly_report(
        today=today,
        portfolio_before=portfolio_before,
        portfolio_after=portfolio,
        prices=prices,
        screener_text=screener_resp.text,
        analyst_full_text=analyst_full_text,
        pm_full_text=pm_resp.text,
        executed_trades=executed_trades,
        risk_violations=risk_violations,
        journal_text=new_journal,
        dry_run=dry_run,
        token_usage=usage_total,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "dryrun" if dry_run else "live"
    report_path = REPORTS_DIR / f"{today.isoformat()}-{suffix}.md"
    report_path.write_text(report_md, encoding="utf-8")
    log.info("Report written to %s", report_path)

    if send_email_flag:
        subject = f"[Investing Agent {'DRY RUN' if dry_run else ''}] Weekly Report {today.isoformat()}"
        send_email(subject=subject.strip(), body_markdown=report_md)

    log.info("=== Weekly cycle complete ===")
    return 0


def _fail_out(msg: str, today: date, dry_run: bool, send_email_flag: bool) -> int:
    log.error("FAIL: %s", msg)
    if send_email_flag:
        try:
            send_email(
                subject=f"[Investing Agent {'DRY RUN' if dry_run else ''}] FAILURE {today}".strip(),
                body_markdown=f"# Pipeline failure\n\n{msg}\n",
            )
        except Exception as e:
            log.error("Also failed to send failure email: %s", e)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly investing-agent cycle")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not mutate portfolio.json or theses.md. Default if neither flag passed.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually execute trades and overwrite theses.md.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip email delivery (still writes a report file).",
    )
    args = parser.parse_args()
    if args.live and args.dry_run:
        parser.error("--live and --dry-run are mutually exclusive")
    dry_run = not args.live  # default = safe
    return run(dry_run=dry_run, send_email_flag=not args.no_email)


if __name__ == "__main__":
    sys.exit(main())
