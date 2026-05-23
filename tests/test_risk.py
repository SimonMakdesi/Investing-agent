"""Unit tests for the risk checker — these enforce CLAUDE.md §4."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.config import STOCKHOLM_TZ
from src.portfolio import Holding, Portfolio, Sleeve
from src.risk import Action, TradeProposal, check_trade


@pytest.fixture
def empty_portfolio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Portfolio:
    from src import portfolio as portfolio_module

    monkeypatch.setattr(portfolio_module, "TRANSACTIONS_LOG", tmp_path / "transactions.log")
    return Portfolio(
        cash_sek=100_000.0,
        holdings={},
        inception_date=datetime.now(tz=STOCKHOLM_TZ),
        initial_capital_sek=100_000.0,
    )


def _proposal(
    ticker="X.ST",
    shares=10,
    price=100,
    sleeve=Sleeve.CORE,
    sector="Industrials",
    action=Action.BUY,
):
    return TradeProposal(
        action=action,
        ticker=ticker,
        shares=shares,
        price=price,
        sleeve=sleeve,
        sector=sector,
        rationale="test",
    )


def test_clean_buy_passes(empty_portfolio: Portfolio):
    violations = check_trade(empty_portfolio, _proposal(shares=10, price=100), prices={}, sector_lookup={})
    assert violations == []


def test_insufficient_cash_blocks(empty_portfolio: Portfolio):
    violations = check_trade(
        empty_portfolio,
        _proposal(shares=10_000, price=100),  # 1M SEK trade with 100k cash
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "insufficient_cash" for v in violations)


def test_max_single_holding_blocks_oversized_buy(empty_portfolio: Portfolio):
    # 20% of portfolio in one name — exceeds 15% cap
    violations = check_trade(
        empty_portfolio,
        _proposal(shares=200, price=100),  # 20k of a 100k portfolio
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "max_single_holding" for v in violations)


def test_max_aggressive_single_blocks_oversized_aggressive(empty_portfolio: Portfolio):
    # 12% in an aggressive position — exceeds 10% aggressive cap (also 15% overall cap is fine)
    violations = check_trade(
        empty_portfolio,
        _proposal(shares=120, price=100, sleeve=Sleeve.AGGRESSIVE),
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "max_aggressive_single" for v in violations)


def test_sector_cap_blocks_when_combined_exceeds_25(empty_portfolio: Portfolio):
    # Pre-populate two existing industrial positions
    empty_portfolio.holdings["A.ST"] = Holding(
        ticker="A.ST", shares=100, avg_cost=100,
        sleeve=Sleeve.CORE, opened_at=datetime.now(tz=STOCKHOLM_TZ),
        sector="Industrials",
    )
    empty_portfolio.holdings["B.ST"] = Holding(
        ticker="B.ST", shares=120, avg_cost=100,
        sleeve=Sleeve.CORE, opened_at=datetime.now(tz=STOCKHOLM_TZ),
        sector="Industrials",
    )
    empty_portfolio.cash_sek = 78_000.0  # 100k - 22k spent

    # Adding another industrial brings sector >25%
    violations = check_trade(
        empty_portfolio,
        _proposal(ticker="C.ST", shares=50, price=100, sector="Industrials"),
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "max_sector" for v in violations)


def test_max_holdings_blocks_eleventh(empty_portfolio: Portfolio):
    # Seed 10 small holdings
    for i in range(10):
        empty_portfolio.holdings[f"T{i}.ST"] = Holding(
            ticker=f"T{i}.ST", shares=10, avg_cost=100,
            sleeve=Sleeve.CORE, opened_at=datetime.now(tz=STOCKHOLM_TZ),
            sector="Industrials",
        )
    empty_portfolio.cash_sek = 90_000.0

    violations = check_trade(
        empty_portfolio,
        _proposal(ticker="NEW.ST", shares=10, price=100),
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "max_holdings" for v in violations)


def test_sell_not_held_blocks(empty_portfolio: Portfolio):
    violations = check_trade(
        empty_portfolio,
        _proposal(action=Action.SELL, ticker="X.ST", shares=10, price=100),
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "not_held" for v in violations)


def test_sell_full_position_before_4_weeks_blocks(empty_portfolio: Portfolio):
    empty_portfolio.holdings["X.ST"] = Holding(
        ticker="X.ST", shares=10, avg_cost=100,
        sleeve=Sleeve.CORE, opened_at=datetime.now(tz=STOCKHOLM_TZ),  # just opened
        sector="Industrials",
    )
    violations = check_trade(
        empty_portfolio,
        _proposal(action=Action.SELL, ticker="X.ST", shares=10, price=120),
        prices={},
        sector_lookup={},
    )
    assert any(v.rule == "min_holding_period" for v in violations)


def test_sell_full_position_after_4_weeks_passes(empty_portfolio: Portfolio):
    old = datetime.now(tz=STOCKHOLM_TZ) - timedelta(days=40)
    empty_portfolio.holdings["X.ST"] = Holding(
        ticker="X.ST", shares=10, avg_cost=100,
        sleeve=Sleeve.CORE, opened_at=old,
        sector="Industrials",
    )
    violations = check_trade(
        empty_portfolio,
        _proposal(action=Action.SELL, ticker="X.ST", shares=10, price=120),
        prices={},
        sector_lookup={},
    )
    assert violations == []


def test_partial_sell_within_4_weeks_passes(empty_portfolio: Portfolio):
    empty_portfolio.holdings["X.ST"] = Holding(
        ticker="X.ST", shares=10, avg_cost=100,
        sleeve=Sleeve.CORE, opened_at=datetime.now(tz=STOCKHOLM_TZ),
        sector="Industrials",
    )
    violations = check_trade(
        empty_portfolio,
        _proposal(action=Action.SELL, ticker="X.ST", shares=4, price=120),
        prices={},
        sector_lookup={},
    )
    assert violations == []
