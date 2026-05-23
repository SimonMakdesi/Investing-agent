"""Unit tests for portfolio math.

Uses a tmp_path-isolated portfolio so the real state/portfolio.json is untouched.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.portfolio import Portfolio, Sleeve


@pytest.fixture
def fresh_portfolio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Portfolio:
    # Redirect the transactions log to tmp_path so tests don't pollute state/
    from src import portfolio as portfolio_module

    monkeypatch.setattr(portfolio_module, "TRANSACTIONS_LOG", tmp_path / "transactions.log")
    return Portfolio(
        cash_sek=100_000.0,
        holdings={},
        inception_date=datetime(2026, 1, 1),
        initial_capital_sek=100_000.0,
    )


def test_initial_state(fresh_portfolio: Portfolio):
    assert fresh_portfolio.cash_sek == 100_000.0
    assert fresh_portfolio.holdings == {}
    assert fresh_portfolio.value(prices={}) == 100_000.0


def test_buy_reduces_cash_and_creates_holding(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    assert fresh_portfolio.cash_sek == 75_000.0
    h = fresh_portfolio.holdings["VOLV-B.ST"]
    assert h.shares == 100
    assert h.avg_cost == 250.0
    assert h.sleeve == Sleeve.CORE


def test_buy_averages_cost_on_existing_position(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=200.0, sleeve=Sleeve.CORE)
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=300.0, sleeve=Sleeve.CORE)
    h = fresh_portfolio.holdings["VOLV-B.ST"]
    assert h.shares == 200
    assert h.avg_cost == 250.0
    assert fresh_portfolio.cash_sek == 50_000.0


def test_buy_rejects_insufficient_cash(fresh_portfolio: Portfolio):
    with pytest.raises(ValueError, match="Insufficient cash"):
        fresh_portfolio.buy("VOLV-B.ST", shares=1000, price=250.0, sleeve=Sleeve.CORE)


def test_buy_rejects_sleeve_mismatch(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("X.ST", shares=10, price=100.0, sleeve=Sleeve.CORE)
    with pytest.raises(ValueError, match="Sleeve mismatch"):
        fresh_portfolio.buy("X.ST", shares=10, price=100.0, sleeve=Sleeve.AGGRESSIVE)


def test_sell_full_position_removes_holding(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    fresh_portfolio.sell("VOLV-B.ST", shares=100, price=300.0)
    assert "VOLV-B.ST" not in fresh_portfolio.holdings
    assert fresh_portfolio.cash_sek == 105_000.0  # 75k + 30k proceeds


def test_sell_partial_keeps_position(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    fresh_portfolio.sell("VOLV-B.ST", shares=40, price=300.0)
    h = fresh_portfolio.holdings["VOLV-B.ST"]
    assert h.shares == 60
    assert h.avg_cost == 250.0  # avg cost unchanged on partial sell
    assert fresh_portfolio.cash_sek == 87_000.0  # 75k + 12k


def test_sell_rejects_more_than_held(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    with pytest.raises(ValueError, match="Cannot sell"):
        fresh_portfolio.sell("VOLV-B.ST", shares=101, price=300.0)


def test_value_uses_provided_prices(fresh_portfolio: Portfolio):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    value = fresh_portfolio.value(prices={"VOLV-B.ST": 300.0})
    assert value == 75_000.0 + 30_000.0


def test_roundtrip_save_load(fresh_portfolio: Portfolio, tmp_path: Path):
    fresh_portfolio.buy("VOLV-B.ST", shares=100, price=250.0, sleeve=Sleeve.CORE)
    path = tmp_path / "portfolio.json"
    fresh_portfolio.save(path)
    loaded = Portfolio.load(path)
    assert loaded.cash_sek == fresh_portfolio.cash_sek
    assert loaded.holdings["VOLV-B.ST"].shares == 100
