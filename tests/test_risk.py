"""Unit tests for the v2 (aggressive, one-book) risk checker — enforces CLAUDE.md §4."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.config import STOCKHOLM_TZ
from src.portfolio import Holding, Portfolio, Sleeve
from src.risk import MAX_HOLDINGS, Action, TradeProposal, check_trade


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


def _buy(ticker="X.ST", shares=10, price=100, sector="Industrials"):
    return TradeProposal(
        action=Action.BUY, ticker=ticker, shares=shares, price=price,
        sector=sector, rationale="test",
    )


def test_clean_buy_passes(empty_portfolio: Portfolio):
    # 20% of book — under the 30% cap.
    assert check_trade(empty_portfolio, _buy(shares=200, price=100), prices={}, sector_lookup={}) == []


def test_insufficient_cash_blocks(empty_portfolio: Portfolio):
    viols = check_trade(empty_portfolio, _buy(shares=10_000, price=100), prices={}, sector_lookup={})
    assert any(v.rule == "insufficient_cash" for v in viols)


def test_single_holding_cap_is_30pct(empty_portfolio: Portfolio):
    # 25% passes (was blocked under the old 15% cap)...
    assert not any(
        v.rule == "max_single_holding"
        for v in check_trade(empty_portfolio, _buy(shares=250, price=100), prices={}, sector_lookup={})
    )
    # ...35% is blocked.
    assert any(
        v.rule == "max_single_holding"
        for v in check_trade(empty_portfolio, _buy(shares=350, price=100), prices={}, sector_lookup={})
    )


def test_sector_cap_is_40pct(empty_portfolio: Portfolio):
    empty_portfolio.holdings["A.ST"] = Holding(
        ticker="A.ST", shares=250, avg_cost=100, sleeve=Sleeve.CORE,
        opened_at=datetime.now(tz=STOCKHOLM_TZ), sector="Industrials",
    )
    empty_portfolio.cash_sek = 75_000.0  # 25% deployed
    # Adding another 25% industrial -> 50% sector > 40% cap.
    viols = check_trade(
        empty_portfolio, _buy(ticker="C.ST", shares=250, price=100, sector="Industrials"),
        prices={}, sector_lookup={},
    )
    assert any(v.rule == "max_sector" for v in viols)


def test_min_cash_buffer_blocks_overdeploy(empty_portfolio: Portfolio):
    # 96% equity in one trade -> trips the 5% cash floor (and single-holding, but
    # we assert the cash rule specifically).
    viols = check_trade(empty_portfolio, _buy(shares=960, price=100), prices={}, sector_lookup={})
    assert any(v.rule == "min_cash_buffer" for v in viols)


def test_max_holdings_cap_is_8(empty_portfolio: Portfolio):
    for i in range(MAX_HOLDINGS):
        empty_portfolio.holdings[f"T{i}.ST"] = Holding(
            ticker=f"T{i}.ST", shares=10, avg_cost=100, sleeve=Sleeve.CORE,
            opened_at=datetime.now(tz=STOCKHOLM_TZ), sector=f"S{i}",
        )
    empty_portfolio.cash_sek = 92_000.0
    viols = check_trade(empty_portfolio, _buy(ticker="NEW.ST", shares=10, price=100, sector="SX"),
                        prices={}, sector_lookup={})
    assert any(v.rule == "max_holdings" for v in viols)


def test_sell_not_held_blocks(empty_portfolio: Portfolio):
    viols = check_trade(
        empty_portfolio,
        TradeProposal(action=Action.SELL, ticker="X.ST", shares=10, price=100, sector="X", rationale="t"),
        prices={}, sector_lookup={},
    )
    assert any(v.rule == "not_held" for v in viols)


def test_oversell_blocked_longonly(empty_portfolio: Portfolio):
    empty_portfolio.holdings["X.ST"] = Holding(
        ticker="X.ST", shares=10, avg_cost=100, sleeve=Sleeve.CORE,
        opened_at=datetime.now(tz=STOCKHOLM_TZ), sector="Industrials",
    )
    viols = check_trade(
        empty_portfolio,
        TradeProposal(action=Action.SELL, ticker="X.ST", shares=20, price=120, sector="Industrials", rationale="t"),
        prices={}, sector_lookup={},
    )
    assert any(v.rule == "oversold" for v in viols)


def test_no_minimum_holding_period(empty_portfolio: Portfolio):
    # v2 removed the 4-week min hold — a full same-day exit is allowed.
    empty_portfolio.holdings["X.ST"] = Holding(
        ticker="X.ST", shares=10, avg_cost=100, sleeve=Sleeve.CORE,
        opened_at=datetime.now(tz=STOCKHOLM_TZ), sector="Industrials",
    )
    viols = check_trade(
        empty_portfolio,
        TradeProposal(action=Action.SELL, ticker="X.ST", shares=10, price=120, sector="Industrials", rationale="rotate"),
        prices={}, sector_lookup={},
    )
    assert viols == []
