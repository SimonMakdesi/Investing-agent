"""Floor pace-line math (CLAUDE.md §2).

+TARGET_RETURN_PCT over TARGET_HORIZON_MONTHS is a FLOOR (minimum ambition),
not a finish line. This module turns it into a simple linear minimum-pace line
so the Trader and reports can see whether the book is above or below that floor
— being ABOVE it is never a reason to de-risk, only to keep compounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.config import TARGET_HORIZON_MONTHS, TARGET_RETURN_PCT
from src.portfolio import Portfolio

_DAYS_PER_MONTH = 30.44


@dataclass(frozen=True)
class Pace:
    actual_return_pct: float       # (current / initial - 1) * 100, since inception
    elapsed_days: int
    horizon_days: float
    target_return_pct: float       # the full-horizon goal (e.g. 50)
    on_pace_return_pct: float      # linear expectation at elapsed_days
    gap_pct: float                 # actual - on_pace (positive = ahead)

    @property
    def status(self) -> str:
        # "Above the floor" is good and means KEEP COMPOUNDING — never de-risk.
        if self.gap_pct > 1.0:
            return "above floor - keep compounding, do not de-risk"
        if self.gap_pct < -1.0:
            return "below floor - make sure capital is fully working"
        return "at floor"

    def one_liner(self) -> str:
        return (
            f"Floor +{self.target_return_pct:.0f}% in {TARGET_HORIZON_MONTHS}mo (a MINIMUM, not a cap). "
            f"Day {self.elapsed_days} of {self.horizon_days:.0f}: actual {self.actual_return_pct:+.1f}% "
            f"vs floor pace {self.on_pace_return_pct:+.1f}% -> {self.status} "
            f"({self.gap_pct:+.1f}pp)."
        )


def time_weighted_return(portfolio: Portfolio, current_value: float) -> float:
    """Contribution-neutral return %, the honest 'investing skill' number.

    Chains the per-segment growth factors between external cash injections so
    that deposits/top-ups never count as performance. With no contributions this
    reduces to (current_value / initial_capital - 1).
    """
    initial = portfolio.initial_capital_sek or 1.0
    twr = 1.0
    seg_start = initial  # value at the start of the current segment
    for c in sorted(portfolio.contributions, key=lambda x: x.date):
        if seg_start > 1e-9:
            twr *= c.value_before_sek / seg_start
        seg_start = c.value_before_sek + c.amount_sek  # cash lands, new segment begins
    if seg_start > 1e-9:
        twr *= current_value / seg_start
    return (twr - 1.0) * 100.0


def compute_pace(portfolio: Portfolio, current_value: float, today: date) -> Pace:
    # Performance is measured contribution-neutral (TWR), so monthly top-ups
    # never flatter the number.
    actual = time_weighted_return(portfolio, current_value)
    elapsed = max(0, (today - portfolio.inception_date.date()).days)
    horizon = TARGET_HORIZON_MONTHS * _DAYS_PER_MONTH
    frac = min(1.0, elapsed / horizon) if horizon else 1.0
    on_pace = TARGET_RETURN_PCT * frac
    return Pace(
        actual_return_pct=actual,
        elapsed_days=elapsed,
        horizon_days=horizon,
        target_return_pct=TARGET_RETURN_PCT,
        on_pace_return_pct=on_pace,
        gap_pct=actual - on_pace,
    )
