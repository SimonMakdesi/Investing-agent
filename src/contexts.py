"""Build the user-message payload for each role (v2: Scout / Analyst / Trader / Journal Keeper).

Keeps formatting logic out of the pipeline orchestrator. Each role's input is
shaped to be as compact as possible — the model never sees raw data the
constitution warns against.
"""

from __future__ import annotations

from datetime import date

from src.config import THESES_FILE
from src.data.insiders import InsiderTransaction
from src.metrics import TickerMetrics
from src.pace import Pace
from src.portfolio import Portfolio
from src.universe import UniverseEntry

# Shared one-block reminder of the hard caps (mirrors CLAUDE.md §4).
RISK_CAPS_BLOCK = (
    "RISK CAPS (one book — enforced in code):\n"
    "  - Max 30% in any single holding (of total)\n"
    "  - Max 40% in any single sector (of total)\n"
    "  - Max ~8 holdings total\n"
    "  - Min ~5% cash (max ~95% equity)\n"
    "  - Long-only: no leverage, shorting, or derivatives\n"
    "  - No minimum holding period or holding count\n"
)


def _read_journal() -> str:
    if not THESES_FILE.exists():
        return "(no prior journal — this is the first cycle)"
    return THESES_FILE.read_text(encoding="utf-8")


def _portfolio_summary(p: Portfolio, prices: dict[str, float], pace: Pace | None = None) -> str:
    total = p.value(prices)
    cash_pct = p.cash_sek / total * 100.0 if total else 0
    if not p.holdings:
        holdings_block = "(no holdings — 100% cash)"
    else:
        lines = []
        for h in p.holdings.values():
            px = prices.get(h.ticker, h.avg_cost)
            value = h.shares * px
            pct = value / total * 100.0 if total else 0
            pnl = (px / h.avg_cost - 1) * 100.0 if h.avg_cost else 0
            age = (date.today() - h.opened_at.date()).days
            lines.append(
                f"  {h.ticker:12s}  shares={h.shares:>7.0f}  cost={h.avg_cost:>8.2f}  "
                f"px={px:>8.2f}  value={value:>10,.0f}  pct={pct:>5.1f}%  "
                f"pnl={pnl:+6.1f}%  sector={h.sector or '?'}  held={age}d"
            )
        holdings_block = "\n".join(lines)
    pace_line = f"\nPACE: {pace.one_liner()}" if pace else ""
    return (
        f"PORTFOLIO  total={total:,.0f} SEK  cash={p.cash_sek:,.0f} SEK ({cash_pct:.1f}%)"
        f"{pace_line}\n"
        f"HOLDINGS ({len(p.holdings)}):\n{holdings_block}"
    )


def _significant_insider_block(
    transactions: list[InsiderTransaction], threshold_sek: float = 500_000
) -> str:
    sig = [t for t in transactions if t.is_buy and t.total_value_sek >= threshold_sek]
    if not sig:
        return "(no significant insider buys recently)"
    sig.sort(key=lambda t: t.total_value_sek, reverse=True)
    lines = [
        f"  {t.publication_date}  {t.issuer[:38]:38s}  {t.person[:24]:24s}  "
        f"{t.total_value_sek:>12,.0f} SEK"
        for t in sig[:25]
    ]
    if len(sig) > 25:
        lines.append(f"  ... and {len(sig) - 25} more")
    return "\n".join(lines)


# --- Scout (daily triage; merges old Screener + Event Monitor) --------------

def scout_user_message(
    today: date,
    deep: bool,
    portfolio: Portfolio,
    prices: dict[str, float],
    metrics: list[TickerMetrics],
    insiders_recent: list[InsiderTransaction],
    watchlist: list[str],
    pace: Pace | None = None,
) -> str:
    metric_lines = "\n".join(m.one_liner() for m in metrics)
    wl = ", ".join(watchlist) if watchlist else "(none)"
    mode = "WEEKLY DEEP scan (full universe, whole-book rotation review)" if deep else "DAILY scan"
    return (
        f"Today: {today.isoformat()}   Mode: {mode}\n\n"
        f"{_portfolio_summary(portfolio, prices, pace)}\n\n"
        f"WATCHLIST (from journal): {wl}\n\n"
        f"UNIVERSE METRICS ({len(metrics)} tickers):\n{metric_lines}\n\n"
        f"SIGNIFICANT INSIDER BUYS (recent, Swedish names, >=500k SEK):\n"
        f"{_significant_insider_block(insiders_recent)}\n"
    )


# --- Analyst (deep per-name note) -------------------------------------------

def analyst_user_message(
    today: date,
    entry: UniverseEntry,
    angle: str,
    metrics: TickerMetrics,
    insider_block: str,
    held_avg_cost: float | None,
    fundamentals_block: str = "  (fundamentals data not available)",
    news_block: str = "  (no news data available)",
    sleeve_hint: str = "either",  # accepted for back-compat; ignored in v2 (one book)
) -> str:
    """Build the Analyst's user-message payload. *_block params are pre-formatted."""
    held_block = (
        f"CURRENTLY HELD: yes, cost basis = {held_avg_cost:.2f} SEK/share"
        if held_avg_cost is not None
        else "CURRENTLY HELD: no"
    )

    ccy = (getattr(entry, "currency", "SEK") or "SEK").upper()
    if ccy != "SEK":
        from src.data import fx
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
        f"{currency_block}"
        f"SCOUT ANGLE: {angle}\n\n"
        f"{held_block}\n\n"
        f"PRICE METRICS:\n  {metrics.one_liner()}\n\n"
        f"FUNDAMENTALS (Börsdata, rolling 12-month):\n{fundamentals_block}\n\n"
        f"INSIDER ACTIVITY (last 90d, equity-program excluded; none for US names):\n{insider_block}\n\n"
        f"RECENT NEWS (last 30 days, materiality 3+ — M1=boilerplate, M5=critical):\n{news_block}\n"
    )


# --- Trader (the decision; merges old PM + Daily PM) ------------------------

def trader_user_message(
    today: date,
    deep: bool,
    portfolio: Portfolio,
    prices: dict[str, float],
    analyst_notes: list[dict],
    analyst_full_text: list[str],
    sell_candidates: list[dict],
    pace: Pace | None = None,
    fx_note: str = "",
) -> str:
    notes_parts = []
    for parsed, full in zip(analyst_notes, analyst_full_text, strict=False):
        notes_parts.append(
            f"--- Analyst note on {parsed.get('ticker', '?')} ({parsed.get('name', '?')}) ---\n{full}\n"
        )
    notes_block = "\n".join(notes_parts) if notes_parts else "(no new candidate notes today)"

    if sell_candidates:
        sells = "\n".join(
            f"  - {c.get('ticker', '?')}: {c.get('reason', '')}" for c in sell_candidates
        )
    else:
        sells = "  (none flagged by the Scout)"

    fx_line = f"\nFX: {fx_note}\n" if fx_note else ""
    mode = "WEEKLY DEEP review (re-examine EVERY holding for rotation)" if deep else "DAILY decision"

    return (
        f"Today: {today.isoformat()}   Mode: {mode}\n\n"
        f"{_portfolio_summary(portfolio, prices, pace)}\n{fx_line}\n"
        f"JOURNAL (current thesis state):\n{_read_journal()}\n\n"
        f"ANALYST NOTES on today's candidates:\n{notes_block}\n\n"
        f"SCOUT SELL / ROTATION FLAGS (held names):\n{sells}\n\n"
        f"{RISK_CAPS_BLOCK}"
    )


# --- Journal Keeper (private memory rewrite) --------------------------------

def journal_keeper_user_message(
    today: date,
    previous_journal: str,
    scout_text: str,
    analyst_full_text: list[str],
    trader_full_text: str,
    executed_trades: list[dict],
    risk_violations: list[dict],
    portfolio_after: Portfolio,
    prices: dict[str, float],
    pace: Pace | None = None,
) -> str:
    if executed_trades:
        trades_block = "\n".join(
            f"  - {t['action']} {t['shares']} {t['ticker']} @ {t['limit_price_sek']:.2f} — {t['rationale']}"
            for t in executed_trades
        )
    else:
        trades_block = "  (no trades executed)"

    if risk_violations:
        viols_block = "\n".join(
            f"  - PROPOSED {v.get('action')} {v.get('shares')} {v.get('ticker')} "
            f"BLOCKED by [{v['rule']}]: {v['detail']}"
            for v in risk_violations
        )
    else:
        viols_block = "  (no proposals blocked)"

    analyst_block = "\n\n".join(analyst_full_text) if analyst_full_text else "(no analyst notes)"

    return (
        f"Today: {today.isoformat()}\n\n"
        f"PREVIOUS JOURNAL:\n{previous_journal}\n\n"
        f"SCOUT OUTPUT THIS CYCLE:\n{scout_text}\n\n"
        f"ANALYST NOTES THIS CYCLE:\n{analyst_block}\n\n"
        f"TRADER DECISION:\n{trader_full_text}\n\n"
        f"EXECUTED TRADES:\n{trades_block}\n\n"
        f"BLOCKED PROPOSALS:\n{viols_block}\n\n"
        f"{_portfolio_summary(portfolio_after, prices, pace)}\n"
    )
