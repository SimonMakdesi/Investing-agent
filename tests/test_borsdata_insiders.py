"""Tests for Borsdata insider summarization (no network — uses synthetic txs)."""

from __future__ import annotations

from datetime import date, timedelta

from src.data.borsdata import InsiderTx
from src.data.borsdata_insiders import summarize


def _tx(amount: float, days_ago: int = 5, equity_program: bool = False,
        owner: str = "Test Person", price: float = 100.0,
        transaction_type: int = 19) -> InsiderTx:
    return InsiderTx(
        ins_id=1,
        owner_name=owner,
        owner_position="director",
        equity_program=equity_program,
        shares=abs(amount) / price if price > 0 else 0,
        price=price,
        amount_sek=amount,
        transaction_type=transaction_type,
        transaction_date=date.today() - timedelta(days=days_ago),
        verification_date=date.today() - timedelta(days=days_ago),
    )


def test_pure_buy():
    s = summarize([_tx(amount=500_000)])
    assert s is not None
    assert s.net_value_sek == 500_000
    assert s.buy_count == 1 and s.sell_count == 0


def test_equity_program_excluded():
    s = summarize([
        _tx(amount=1_000_000),
        _tx(amount=500_000, equity_program=True),  # should be ignored
    ])
    assert s.net_value_sek == 1_000_000
    assert s.buy_count == 1


def test_net_buy_vs_sell():
    s = summarize([
        _tx(amount=1_000_000, owner="A"),
        _tx(amount=-300_000, owner="B"),
    ])
    assert s.gross_buy_value_sek == 1_000_000
    assert s.gross_sell_value_sek == 300_000  # positive magnitude
    assert s.net_value_sek == 700_000
    assert s.distinct_buyers == 1
    assert s.distinct_sellers == 1


def test_window_filtering():
    s = summarize([
        _tx(amount=1_000_000, days_ago=5),
        _tx(amount=2_000_000, days_ago=180),  # outside 90-day window
    ])
    assert s.net_value_sek == 1_000_000
    assert s.buy_count == 1


def test_implausible_price_excluded():
    # Type 3 records with mis-mapped price (price = total value)
    weird = _tx(amount=22_500_000_000_000, price=150_000_000)
    legit = _tx(amount=500_000, price=100)
    s = summarize([weird, legit])
    assert s.buy_count == 1  # only the legit one counts
    assert s.net_value_sek == 500_000


def test_implausibly_large_amount_excluded():
    # Even with sane-looking price, a 100bn SEK trade by one insider is a data error
    huge = _tx(amount=100_000_000_000, price=200)
    legit = _tx(amount=500_000, price=100)
    s = summarize([huge, legit])
    assert s.buy_count == 1
    assert s.net_value_sek == 500_000


def test_largest_single_buy_identified():
    s = summarize([
        _tx(amount=200_000, owner="Small"),
        _tx(amount=5_000_000, owner="Big Buyer"),
        _tx(amount=1_000_000, owner="Medium"),
    ])
    assert s.largest_single_buy_sek == 5_000_000
    assert s.largest_single_buy_owner == "Big Buyer"


def test_empty_input():
    assert summarize([]) is None


def test_only_equity_program_in_window():
    s = summarize([_tx(amount=1_000_000, equity_program=True)])
    assert s is not None
    assert s.buy_count == 0 and s.sell_count == 0
    assert s.net_value_sek == 0
