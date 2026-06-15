"""Blended OMXS30 + S&P 500 benchmark math."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.data import benchmark
from src.portfolio import Holding, Portfolio, Sleeve


def test_regional_weights_flat_portfolio_is_5050():
    p = Portfolio(cash_sek=100_000.0, holdings={}, inception_date=datetime(2026, 1, 1),
                  initial_capital_sek=100_000.0)
    assert benchmark.regional_weights(p, {}) == (0.5, 0.5)


def test_regional_weights_by_currency():
    now = datetime(2026, 1, 1)
    p = Portfolio(
        cash_sek=0.0,
        holdings={
            "VOLV-B.ST": Holding(ticker="VOLV-B.ST", shares=100, avg_cost=300.0,
                                 sleeve=Sleeve.CORE, opened_at=now, currency="SEK"),
            "AAPL": Holding(ticker="AAPL", shares=10, avg_cost=2000.0,
                            sleeve=Sleeve.CORE, opened_at=now, currency="USD"),
        },
        inception_date=now, initial_capital_sek=100_000.0,
    )
    # SE equity = 30000 SEK, US equity = 20000 SEK -> 0.6 / 0.4
    se_w, us_w = benchmark.regional_weights(p, {})
    assert se_w == pytest.approx(0.6)
    assert us_w == pytest.approx(0.4)


def test_blended_index_normalises_and_weights(monkeypatch):
    d0, d1 = date(2026, 1, 1), date(2026, 1, 2)
    hists = {
        benchmark.OMX_TICKER: {d0: 100.0, d1: 110.0},     # +10%
        benchmark.SP500_TICKER: {d0: 50.0, d1: 55.0},     # +10% in USD
        benchmark.USDSEK_TICKER: {d0: 10.0, d1: 10.0},    # flat FX
    }
    monkeypatch.setattr(benchmark, "_close_hist", lambda t, period="2y": hists[t])

    out = benchmark.blended_index_hist(0.5, 0.5)
    assert out[d0] == pytest.approx(1.0)          # base day
    assert out[d1] == pytest.approx(1.10)         # both +10% -> blend +10%


def test_blended_index_fx_move_flows_into_us_leg(monkeypatch):
    d0, d1 = date(2026, 1, 1), date(2026, 1, 2)
    hists = {
        benchmark.OMX_TICKER: {d0: 100.0, d1: 100.0},     # flat
        benchmark.SP500_TICKER: {d0: 50.0, d1: 50.0},     # flat in USD
        benchmark.USDSEK_TICKER: {d0: 10.0, d1: 11.0},    # +10% FX -> US leg +10% in SEK
    }
    monkeypatch.setattr(benchmark, "_close_hist", lambda t, period="2y": hists[t])

    out = benchmark.blended_index_hist(0.0, 1.0)  # 100% US
    assert out[d1] == pytest.approx(1.10)         # pure FX gain shows in SEK terms
