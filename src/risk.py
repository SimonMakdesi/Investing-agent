"""Risk checker — enforces CLAUDE.md §4 hard rules in code.

v2 (aggressive, one-book): the Trader (Claude) proposes trades; this module is
the final gate. Any violation here aborts execution. Claude cannot bypass these
rules because they live in Python, not in prompt text.

The sleeves are gone. Every position competes in a single book. The caps are
looser than v1 on purpose (the owner runs this as an aggressive capability
test), but they are still HARD limits:

  - Max 30% in any single holding (of total portfolio)
  - Max 40% in any single sector (of total portfolio)
  - Max ~8 holdings total
  - Min ~5% cash (so max ~95% equity)
  - No minimum holding period, no minimum holding count
  - Long-only: a SELL can never exceed the held quantity; no shorting/leverage
    is representable in the portfolio engine at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.portfolio import Portfolio, Sleeve

# Hard caps from CLAUDE.md §4 (v2)
MAX_SINGLE_HOLDING_PCT = 30.0
MAX_SECTOR_PCT = 40.0
MAX_HOLDINGS = 8
MIN_CASH_PCT = 5.0          # min cash buffer of total portfolio
MAX_TOTAL_EQUITY_PCT = 95.0  # == 100 - MIN_CASH_PCT


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class TradeProposal:
    action: Action
    ticker: str
    shares: float
    price: float
    sector: str
    rationale: str
    # Kept for backward-compatibility with callers that still pass a sleeve.
    # v2 is one book, so this is ignored by the risk checks.
    sleeve: Sleeve = Sleeve.CORE
    # v1 left this around to override the (now-removed) minimum holding period.
    # There is no min-hold in v2, so it has no effect; kept so existing callers
    # that set it don't break.
    thesis_break: bool = False


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

    # 1. Cash sufficiency — long-only: you can only buy with cash on hand.
    if cost > portfolio.cash_sek + 1e-6:
        violations.append(
            Violation(
                "insufficient_cash",
                f"Need {cost:,.0f} SEK, have {portfolio.cash_sek:,.0f} SEK",
            )
        )
        return violations  # everything else is moot if we can't pay

    # Project the post-trade portfolio (don't mutate the real one)
    post_holdings = {t: h.shares for t, h in portfolio.holdings.items()}
    post_holdings[proposal.ticker] = post_holdings.get(proposal.ticker, 0.0) + proposal.shares
    post_cash = portfolio.cash_sek - cost

    def value_of(ticker: str, shares: float) -> float:
        if ticker in prices:
            return shares * prices[ticker]
        h = portfolio.holdings.get(ticker)
        if h:
            return shares * h.avg_cost
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

    # 3. Max single holding (30% of total)
    pct = new_position_value / total_value * 100.0 if total_value else 0.0
    if pct > MAX_SINGLE_HOLDING_PCT + 1e-6:
        violations.append(
            Violation(
                "max_single_holding",
                f"{proposal.ticker} would be {pct:.1f}% of portfolio (cap {MAX_SINGLE_HOLDING_PCT:.0f}%)",
            )
        )

    # 4. Sector cap (40%)
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
    sector_pct = sector_value / total_value * 100.0 if total_value else 0.0
    if sector_pct > MAX_SECTOR_PCT + 1e-6:
        violations.append(
            Violation(
                "max_sector",
                f"Sector '{sector}' would be {sector_pct:.1f}% (cap {MAX_SECTOR_PCT:.0f}%)",
            )
        )

    # 5. Total equity exposure (max 95% -> min 5% cash)
    total_equity = sum(value_of(t, s) for t, s in post_holdings.items())
    equity_pct = total_equity / total_value * 100.0 if total_value else 0.0
    if equity_pct > MAX_TOTAL_EQUITY_PCT + 1e-6:
        violations.append(
            Violation(
                "min_cash_buffer",
                f"Equity would be {equity_pct:.1f}% — leaves <{MIN_CASH_PCT:.0f}% cash "
                f"(cap {MAX_TOTAL_EQUITY_PCT:.0f}% equity)",
            )
        )

    return violations


def _check_sell(portfolio: Portfolio, proposal: TradeProposal) -> list[Violation]:
    violations: list[Violation] = []
    holding = portfolio.holdings.get(proposal.ticker)
    if not holding:
        violations.append(Violation("not_held", f"Cannot sell {proposal.ticker}: not held"))
        return violations
    # Long-only invariant: cannot sell more than held (no shorting).
    if proposal.shares > holding.shares + 1e-9:
        violations.append(
            Violation(
                "oversold",
                f"Sell of {proposal.shares} exceeds holding {holding.shares} of {proposal.ticker} "
                "(long-only: no shorting)",
            )
        )
    # No minimum holding period in v2 — same-day rotation is allowed by design.
    return violations
