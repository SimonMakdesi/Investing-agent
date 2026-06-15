"""Unit tests for FX normalisation to SEK."""

from __future__ import annotations

from datetime import date

import pytest

from src.data import fx
from src.data.prices import PriceSnapshot


@pytest.fixture(autouse=True)
def _clear_fx_cache():
    # Isolate each test from cached rates / real network.
    fx._cache.clear()
    fx._cache["SEK"] = 1.0
    yield
    fx._cache.clear()
    fx._cache["SEK"] = 1.0


def test_sek_is_identity():
    assert fx.rate("SEK") == 1.0
    assert fx.to_sek(123.45, "SEK") == 123.45


def test_to_sek_uses_cached_rate():
    fx._cache["USD"] = 10.0
    assert fx.to_sek(5.0, "USD") == 50.0


def test_rate_is_case_insensitive():
    fx._cache["USD"] = 10.0
    assert fx.rate("usd") == 10.0


def test_get_latest_closes_sek_converts(monkeypatch):
    from src.data import prices as prices_mod

    fake = {
        "AAPL": PriceSnapshot("AAPL", date(2026, 6, 15), 200.0, "USD"),
        "VOLV-B.ST": PriceSnapshot("VOLV-B.ST", date(2026, 6, 15), 300.0, "SEK"),
    }
    monkeypatch.setattr(prices_mod, "get_latest_closes", lambda tickers: fake)
    fx._cache["USD"] = 10.5

    out = prices_mod.get_latest_closes_sek(["AAPL", "VOLV-B.ST"])
    assert out["AAPL"] == pytest.approx(2100.0)   # 200 USD * 10.5
    assert out["VOLV-B.ST"] == pytest.approx(300.0)  # already SEK
