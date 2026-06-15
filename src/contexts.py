"""Build the user-message payload for each role.

Keeps the formatting logic out of the pipeline orchestrator.
Each role's input is shaped to be as compact as possible — the goal
is for the model to never see raw data the constitution warns against.
"""

from __future__ import annotations

from datetime import date

from src.config import THESES_FILE
from src.data.insiders import InsiderTransaction
from src.metrics import TickerMetrics
from src.portfolio import Portfolio
from src.universe import UniverseEntry


def _read_journal() -> str:
    if not THESES_FILE.exists():
        return "(no prior journal — this is the first cycle)"
    return THESES_FILE.read_text(encoding="utf-8")


def _portfolio_summary(p: Portfolio, prices: dict[str, float]) -> str:
    total = p.value(prices)
    cash_pct = p.cash_sek / total * 100.0 if total else 0
    if not p.holdings:
        holdings_block = "(no holdings)"
    else:
        lines = []
        for h in p.holdings.values():
            px = prices.get(h.ticker, h.avg_cost)
            value = h.shares * px
            pct = value / total * 100.0 if total else 0
            lines.append(
                f"  {h.ticker:12s}  shares={h.shares:>6.0f}  "
                f"cost={h.avg_cost:>7.2f}  px={px:>7.2f}  "
                f"value={value:>10,.0f}  pct={pct:>5.1f}%  "
                f"sleeve={h.sleeve.value}  sector={h.sector or '?'}  "
                f"opened={h.opened_at.date().isoformat()}"
            )
        holdings_block = "\n".join(lines)
    return (
        f"PORTFOLIO  total={total:,.0f} SEK  cash={p.cash_sek:,.0f} SEK ({cash_pct:.1f}%)\n"
        f"HOLDINGS ({len(p.holdings)}):\n{holdings_block}"
    )


def _significant_insider_block(
    transactions: list[InsiderTransaction], threshold_sek: float = 500_000
) -> str:
    sig = [t for t in transactions if t.is_buy and t.total_value_sek >= threshold_sek]
    if not sig:
        return "(no significant insider buys in the last 7 days)"
    lines = []
    # Sort largest first, top 25 to cap token cost
    sig.sort(key=lambda t: t.total_value_sek, reverse=True)
    for t in sig[:25]:
        lines.append(
            f"  {t.publication_date}  {t.issuer[:38]:38s}  "
            f"{t.person[:24]:24s}  {t.total_value_sek:>12,.0f} SEK"
        )
    if len(sig) > 25:
        lines.append(f"  ... and {len(sig) - 25} more")
    return "\n".join(lines)


def screener_user_message(
    today: date,
    portfolio: Portfolio,
    prices: dict[str, float],
    metrics: list[TickerMetrics],
    insiders_7d: list[InsiderTransaction],
) -> str:
    metric_lines = "\n".join(m.one_liner() for m in metrics)
    return (
        f"Today: {today.isoformat()}\n\n"
        f"{_portfolio_summary(portfolio, prices)}\n\n"
        f"JOURNAL (previous week):\n{_read_journal()}\n\n"
        f"UNIVERSE METRICS ({len(metrics)} tickers):\n{metric_lines}\n\n"
        f"SIGNIFICANT INSIDER BUYS (last 7d, >=500k SEK):\n"
        f"{_significant_insider_block(insiders_7d)}\n"
    )


def analyst_user_message(
    today: date,
    entry: UniverseEntry,
    angle: str,
    sleeve_hint: str,
    metrics: TickerMetrics,
    insider_block: str,
    held_avg_cost: float | None,
    fundamentals_block: str = "  (fundamentals data not available)",
    news_block: str = "  (no news data available)",
) -> str:
    """Build the Analyst's user-message payload.

    All *_block params are pre-formatted text from the dedicated data modules.
    """
    held_block = (
        f"CURRENTLY HELD: yes, cost basis = {held_avg_cost:.2f} SEK/share"
        if held_avg_cost is not None
        else "CURRENTLY HELD: no"
    )

    ccy = (getattr(entry, "currency", "SEK") or "SEK").upper()
    if ccy != "SEK":
        from src.data import fx  # local import keeps module load cheap
        currency_block = (
            f"CURRENCY: {ccy} — metrics & fundamentals below are in {ccy}. "
            f"1 {ccy} = {fx.rate(ccy):.2f} SEK today; the portfolio books this position in SEK at that rate.\n"
        )
    else:
        currency_block = "CURRENCY: SEK\n"

    return (
        f"Today: {today.isoformat()}\n\n"
        f"COMPANY: {entry.name} ({entry.ticker})\n"
        f"SECTOR: {entry.sector}\n"
        f"TIER: {entry.tier.value}\n"
        f"{currency_block}"
        f"SCREENER ANGLE: {angle}\n"
        f"SLEEVE HINT FROM SCREENER: {sleeve_hint}\n\n"
        f"{held_block}\n\n"
        f"PRICE METRICS:\n  {metrics.one_liner()}\n\n"
        f"FUNDAMENTALS (Börsdata, rolling 12-month):\n{fundamentals_block}\n\n"
        f"INSIDER ACTIVITY (last 90d, equity-program transactions excluded):\n{insider_block}\n\n"
        f"RECENT NEWS (last 30 days, materiality 3+ — M1=boilerplate, M5=critical):\n{news_block}\n\n"
        f"DOSSIER (prior notes):\n(no dossier on file yet — this is the first time we look at this name)\n"
    )


def portfolio_manager_user_message(
    today: date,
    portfolio: Portfolio,
    prices: dict[str, float],
    analyst_notes: list[dict],  # each is the parsed JSON from analyst.json
    analyst_full_text: list[str],  # the full prose notes, in same order
) -> str:
    notes_block_parts = []
    for parsed, full in zip(analyst_notes, analyst_full_text, strict=False):
        notes_block_parts.append(
            f"--- Analyst note on {parsed.get('ticker', '?')} ({parsed.get('name', '?')}) ---\n"
            f"{full}\n"
        )
    notes_block = "\n".join(notes_block_parts) if notes_block_parts else "(no analyst notes this week)"

    return (
        f"Today: {today.isoformat()}\n\n"
        f"{_portfolio_summary(portfolio, prices)}\n\n"
        f"JOURNAL (previous week):\n{_read_journal()}\n\n"
        f"ANALYST NOTES THIS WEEK:\n{notes_block}\n\n"
        f"RISK CAPS (mirrors constitution §4 — enforced in code):\n"
        f"  - Max 15% single holding (of total portfolio)\n"
        f"  - Max 25% any single sector (of total)\n"
        f"  - Max 10% single Aggressive position\n"
        f"  - Max 20% Aggressive sleeve total\n"
        f"  - Min 30% cash buffer of the Core sleeve\n"
        f"  - Max 90% total equity exposure\n"
        f"  - Max ~10 holdings\n"
        f"  - Min 4-week holding period (full exits)\n"
    )


def daily_pm_user_message(
    today: date,
    portfolio: Portfolio,
    prices: dict[str, float],
    journal: str,
    triggers_block: str,
    analyst_full_text: list[str],
    fx_note: str = "",
) -> str:
    """Build the Daily PM's user-message payload.

    `triggers_block` is pre-formatted text describing the material events that
    caused the escalation (flags, insider activity, movers with news).
    `analyst_full_text` holds fresh Analyst notes for any not-yet-held names the
    triggers surfaced (empty for pure manage/exit days). `fx_note` carries the
    USD→SEK rate line when US names are in play.
    """
    if analyst_full_text:
        notes_block = "\n\n".join(analyst_full_text)
    else:
        notes_block = "(no new-name analysis today — triggers are on held names only)"

    fx_line = f"\nFX: {fx_note}\n" if fx_note else ""

    return (
        f"Today: {today.isoformat()}\n\n"
        f"{_portfolio_summary(portfolio, prices)}\n{fx_line}\n"
        f"JOURNAL (current thesis state):\n{_read_journal()}\n\n"
        f"TODAY'S TRIGGERS (why you were invoked):\n{triggers_block}\n\n"
        f"ANALYST NOTES on new (not-yet-held) names surfaced today:\n{notes_block}\n\n"
        f"RISK CAPS (mirrors constitution §4 — enforced in code):\n"
        f"  - Max 15% single holding (of total portfolio)\n"
        f"  - Max 25% any single sector (of total)\n"
        f"  - Max 10% single Aggressive position\n"
        f"  - Max 20% Aggressive sleeve total\n"
        f"  - Min 30% cash buffer of the Core sleeve\n"
        f"  - Max 90% total equity exposure\n"
        f"  - Max ~10 holdings\n"
        f"  - Min 4-week holding period — early full exit requires thesis_break=True + rationale\n"
    )


def journal_keeper_user_message(
    today: date,
    previous_journal: str,
    screener_text: str,
    analyst_full_text: list[str],
    pm_full_text: str,
    executed_trades: list[dict],
    risk_violations: list[dict],
    portfolio_after: Portfolio,
    prices: dict[str, float],
) -> str:
    if executed_trades:
        trades_block = "\n".join(
            f"  - {t['action']} {t['shares']} {t['ticker']} @ {t['limit_price_sek']:.2f} ({t['sleeve']}) — {t['rationale']}"
            for t in executed_trades
        )
    else:
        trades_block = "  (no trades executed)"

    if risk_violations:
        viols_block = "\n".join(
            f"  - PROPOSED {v['action']} {v['shares']} {v['ticker']} "
            f"BLOCKED by [{v['rule']}]: {v['detail']}"
            for v in risk_violations
        )
    else:
        viols_block = "  (no proposals blocked)"

    analyst_block = "\n\n".join(analyst_full_text) if analyst_full_text else "(no analyst notes)"

    return (
        f"Today: {today.isoformat()}\n\n"
        f"PREVIOUS JOURNAL:\n{previous_journal}\n\n"
        f"SCREENER OUTPUT THIS WEEK:\n{screener_text}\n\n"
        f"ANALYST NOTES THIS WEEK:\n{analyst_block}\n\n"
        f"PORTFOLIO MANAGER DECISION:\n{pm_full_text}\n\n"
        f"EXECUTED TRADES:\n{trades_block}\n\n"
        f"BLOCKED PROPOSALS (risk violations):\n{viols_block}\n\n"
        f"{_portfolio_summary(portfolio_after, prices)}\n"
    )


def event_monitor_user_message(
    today: date,
    portfolio: Portfolio,
    prices: dict[str, float],
    journal: str,
    insiders_today: list[InsiderTransaction],
    large_movers: list[tuple[str, float]],  # (ticker, daily_change_pct)
    news_block: str = "  (no notable news in the last 24 hours)",
) -> str:
    if portfolio.holdings:
        hold_lines = "\n".join(
            f"  {h.ticker:12s}  {h.shares:>6.0f}sh  cost={h.avg_cost:>7.2f}  "
            f"px={prices.get(h.ticker, h.avg_cost):>7.2f}  sleeve={h.sleeve.value}"
            for h in portfolio.holdings.values()
        )
    else:
        hold_lines = "  (no holdings)"

    if insiders_today:
        insider_lines = "\n".join(
            f"  {t.publication_date}  {t.issuer[:36]:36s}  "
            f"{t.transaction_type:18s}  {t.total_value_sek:>12,.0f} SEK"
            for t in insiders_today
        )
    else:
        insider_lines = "  (no insider transactions on held or watchlist names today)"

    if large_movers:
        mover_lines = "\n".join(f"  {t:12s}  {pct:+.1f}%" for t, pct in large_movers)
    else:
        mover_lines = "  (no held or watchlist tickers moved >=5% today)"

    return (
        f"Today: {today.isoformat()}\n\n"
        f"HOLDINGS:\n{hold_lines}\n\n"
        f"JOURNAL (current thesis state):\n{journal}\n\n"
        f"INSIDER ACTIVITY TODAY on held/watchlist names:\n{insider_lines}\n\n"
        f"LARGE PRICE MOVES on held/watchlist names today:\n{mover_lines}\n\n"
        f"NEWS in the last 24h on held/watchlist names (materiality 3+):\n{news_block}\n"
    )
