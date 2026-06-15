"""The thesis-break override on the 4-week minimum holding period."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.config import STOCKHOLM_TZ
from src.portfolio import Holding, Portfolio, Sleeve
from src.risk import Action, TradeProposal, check_trade


def _portfolio_with_fresh_holding() -> Portfolio:
    now = datetime.now(tz=STOCKHOLM_TZ)
    return Portfolio(
        cash_sek=50_000.0,
        holdings={
            "VOLV-B.ST": Holding(
                ticker="VOLV-B.ST", shares=100, avg_cost=250.0,
                sleeve=Sleeve.CORE, opened_at=now, sector="Industrials",
            )
        },
        inception_date=now,
        initial_capital_sek=100_000.0,
    )


def _full_exit(thesis_break: bool, rationale: str) -> TradeProposal:
    return TradeProposal(
        action=Action.SELL, ticker="VOLV-B.ST", shares=100, price=250.0,
        sleeve=Sleeve.CORE, sector="Industrials", rationale=rationale,
        thesis_break=thesis_break,
    )


def test_full_exit_inside_4_weeks_blocked_without_override():
    p = _portfolio_with_fresh_holding()
    viols = check_trade(p, _full_exit(False, "changed my mind"), prices={}, sector_lookup={})
    assert any(v.rule == "min_holding_period" for v in viols)


def test_thesis_break_override_allows_early_full_exit():
    p = _portfolio_with_fresh_holding()
    viols = check_trade(
        p, _full_exit(True, "profit warning this morning — order intake collapsed"),
        prices={}, sector_lookup={},
    )
    assert not any(v.rule == "min_holding_period" for v in viols)


def test_override_requires_nonempty_rationale():
    p = _portfolio_with_fresh_holding()
    viols = check_trade(p, _full_exit(True, "   "), prices={}, sector_lookup={})
    assert any(v.rule == "min_holding_period" for v in viols)


def test_partial_trim_inside_4_weeks_always_allowed():
    p = _portfolio_with_fresh_holding()
    trim = TradeProposal(
        action=Action.SELL, ticker="VOLV-B.ST", shares=40, price=250.0,
        sleeve=Sleeve.CORE, sector="Industrials", rationale="trim", thesis_break=False,
    )
    viols = check_trade(p, trim, prices={}, sector_lookup={})
    assert not any(v.rule == "min_holding_period" for v in viols)
