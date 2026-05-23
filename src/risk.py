"""Risk checker — enforces CLAUDE.md §4 hard rules in code.

The Portfolio Manager (Claude) proposes trades; this module is the
final gate. Any violation here aborts execution. Claude cannot bypass
these rules because they live in Python, not in prompt text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.config import STOCKHOLM_TZ
from src.portfolio import Portfolio, Sleeve

# Hard caps from CLAUDE.md §4
MAX_SINGLE_HOLDING_PCT = 15.0
MAX_SECTOR_PCT = 25.0
MAX_AGGRESSIVE_SINGLE_PCT = 10.0
MAX_AGGRESSIVE_SLEEVE_PCT = 20.0
MIN_CORE_CASH_PCT_OF_SLEEVE = 30.0  # cash buffer of the core sleeve itself
MAX_TOTAL_EQUITY_PCT = 90.0
MAX_HOLDINGS = 10
MIN_HOLDING_PERIOD_DAYS = 28  # 4 weeks


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class TradeProposal:
    action: Action
    ticker: str
    shares: float
    price: float
    sleeve: Sleeve
    sector: str
    rationale: str


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.detail}"


def check_trade(
    portfolio: Portfolio,
    proposal: TradeProposal,
    prices: dict[str, float],
    sector_lookup: dict[str, str],
) -> list[Violation]:
    """Return a list of violated rules for the proposed trade. Empty list = OK.

    `sector_lookup` maps ticker -> sector for currently-held positions whose
    sector isn't already stored on the Holding.
    """
    violations: list[Violation] = []

    # Mechanical sanity
    if proposal.shares <= 0 or proposal.price <= 0:
        violations.append(Violation("sanity", "shares and price must be positive"))
        return violations

    if proposal.action == Action.BUY:
        violations.extend(_check_buy(portfolio, proposal, prices, sector_lookup))
    else:
        violations.extend(_check_sell(portfolio, proposal))

    return violations


def _check_buy(
    portfolio: Portfolio,
    proposal: TradeProposal,
    prices: dict[str, float],
    sector_lookup: dict[str, str],
) -> list[Violation]:
    violations: list[Violation] = []
    cost = proposal.shares * proposal.price

    # 1. Cash sufficiency
    if cost > portfolio.cash_sek + 1e-6:
        violations.append(
            Violation(
                "insufficient_cash",
                f"Need {cost:,.0f} SEK, have {portfolio.cash_sek:,.0f} SEK",
            )
        )
        return violations  # everything else is moot if we can't pay

    # Project the post-trade portfolio mentally (don't mutate the real one)
    post_holdings = {t: h.shares for t, h in portfolio.holdings.items()}
    post_holdings[proposal.ticker] = post_holdings.get(proposal.ticker, 0.0) + proposal.shares
    post_cash = portfolio.cash_sek - cost

    # Total value uses current prices where known, else avg cost
    def value_of(ticker: str, shares: float) -> float:
        if ticker in prices:
            return shares * prices[ticker]
        h = portfolio.holdings.get(ticker)
        if h:
            return shares * h.avg_cost
        # Newly added position with no existing avg_cost — use the trade price
        return shares * proposal.price

    total_value = post_cash + sum(value_of(t, s) for t, s in post_holdings.items())
    new_position_value = value_of(proposal.ticker, post_holdings[proposal.ticker])

    # 2. Holding count cap
    if proposal.ticker not in portfolio.holdings and len(post_holdings) > MAX_HOLDINGS:
        violations.append(
            Violation(
                "max_holdings",
                f"Would result in {len(post_holdings)} holdings (cap {MAX_HOLDINGS})",
            )
        )

    # 3. Max single holding (15% of total)
    pct = new_position_value / total_value * 100.0
    if pct > MAX_SINGLE_HOLDING_PCT + 1e-6:
        violations.append(
            Violation(
                "max_single_holding",
                f"{proposal.ticker} would be {pct:.1f}% of portfolio (cap {MAX_SINGLE_HOLDING_PCT}%)",
            )
        )

    # 4. Max aggressive single (10% of total) for aggressive trades
    if proposal.sleeve == Sleeve.AGGRESSIVE and pct > MAX_AGGRESSIVE_SINGLE_PCT + 1e-6:
        violations.append(
            Violation(
                "max_aggressive_single",
                f"{proposal.ticker} (aggressive) would be {pct:.1f}% "
                f"(cap {MAX_AGGRESSIVE_SINGLE_PCT}%)",
            )
        )

    # 5. Sector cap (25%)
    sector = proposal.sector
    sector_value = 0.0
    for t, s in post_holdings.items():
        t_sector = (
            sector
            if t == proposal.ticker
            else (portfolio.holdings[t].sector if t in portfolio.holdings else None)
            or sector_lookup.get(t, "Unknown")
        )
        if t_sector == sector:
            sector_value += value_of(t, s)
    sector_pct = sector_value / total_value * 100.0
    if sector_pct > MAX_SECTOR_PCT + 1e-6:
        violations.append(
            Violation(
                "max_sector",
                f"Sector '{sector}' would be {sector_pct:.1f}% (cap {MAX_SECTOR_PCT}%)",
            )
        )

    # 6. Aggressive sleeve cap (20%)
    aggressive_value = 0.0
    for t, s in post_holdings.items():
        t_sleeve = (
            proposal.sleeve
            if t == proposal.ticker
            else (portfolio.holdings[t].sleeve if t in portfolio.holdings else None)
        )
        if t_sleeve == Sleeve.AGGRESSIVE:
            aggressive_value += value_of(t, s)
    aggressive_pct = aggressive_value / total_value * 100.0
    if aggressive_pct > MAX_AGGRESSIVE_SLEEVE_PCT + 1e-6:
        violations.append(
            Violation(
                "max_aggressive_sleeve",
                f"Aggressive sleeve would be {aggressive_pct:.1f}% (cap {MAX_AGGRESSIVE_SLEEVE_PCT}%)",
            )
        )

    # 7. Total equity exposure (max 90%)
    total_equity = sum(value_of(t, s) for t, s in post_holdings.items())
    equity_pct = total_equity / total_value * 100.0
    if equity_pct > MAX_TOTAL_EQUITY_PCT + 1e-6:
        violations.append(
            Violation(
                "max_total_equity",
                f"Equity exposure would be {equity_pct:.1f}% (cap {MAX_TOTAL_EQUITY_PCT}%)",
            )
        )

    # 8. Core sleeve cash buffer (≥30% of core sleeve as cash)
    # Interpreted as: cash should be at least 30% of (cash + core equity)
    core_equity = 0.0
    for t, s in post_holdings.items():
        t_sleeve = (
            proposal.sleeve
            if t == proposal.ticker
            else (portfolio.holdings[t].sleeve if t in portfolio.holdings else Sleeve.CORE)
        )
        if t_sleeve == Sleeve.CORE:
            core_equity += value_of(t, s)
    core_sleeve_total = core_equity + post_cash
    if core_sleeve_total > 0:
        core_cash_pct = post_cash / core_sleeve_total * 100.0
        if core_cash_pct < MIN_CORE_CASH_PCT_OF_SLEEVE - 1e-6:
            violations.append(
                Violation(
                    "core_cash_buffer",
                    f"Cash would be {core_cash_pct:.1f}% of core sleeve "
                    f"(min {MIN_CORE_CASH_PCT_OF_SLEEVE}%)",
                )
            )

    return violations


def _check_sell(portfolio: Portfolio, proposal: TradeProposal) -> list[Violation]:
    violations: list[Violation] = []
    holding = portfolio.holdings.get(proposal.ticker)
    if not holding:
        violations.append(Violation("not_held", f"Cannot sell {proposal.ticker}: not held"))
        return violations
    if proposal.shares > holding.shares + 1e-9:
        violations.append(
            Violation(
                "oversold",
                f"Sell of {proposal.shares} exceeds holding {holding.shares} of {proposal.ticker}",
            )
        )

    # Min holding period — only enforced when selling *all* (partial trims allowed)
    now = datetime.now(tz=STOCKHOLM_TZ)
    age = now - holding.opened_at
    if age < timedelta(days=MIN_HOLDING_PERIOD_DAYS) and abs(proposal.shares - holding.shares) < 1e-6:
        violations.append(
            Violation(
                "min_holding_period",
                f"{proposal.ticker} held only {age.days}d (min {MIN_HOLDING_PERIOD_DAYS}d). "
                "Override requires explicit thesis-break note.",
            )
        )

    return violations
