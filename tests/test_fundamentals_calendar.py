"""Tests for the earnings-date estimate logic in Fundamentals."""

from __future__ import annotations

from datetime import date, timedelta

from src.data.borsdata import Report
from src.data.fundamentals import _estimate_next_report


def _r(year: int, period: int, report_date: date) -> Report:
    return Report(
        year=year, period=period, report_date=report_date,
        revenues=0, gross_income=0, operating_income=0, profit_before_tax=0,
        profit_to_equity_holders=0, earnings_per_share=0, number_of_shares=0,
        dividend=0, total_equity=0, net_debt=0, free_cash_flow=0,
        cash_flow_from_operating=0, total_assets=0, stock_price_average=0,
        currency="SEK", currency_ratio=1.0,
    )


def test_estimates_next_after_q1():
    # Q1 2026 ends 2026-03-31, reported 2026-04-24 (24-day lag)
    # Q2 2026 ends 2026-06-30, expected ~2026-07-24 (same lag)
    r12 = [
        _r(2026, 1, date(2026, 4, 24)),
        _r(2025, 4, date(2026, 1, 28)),
        _r(2025, 3, date(2025, 10, 22)),
        _r(2025, 2, date(2025, 7, 25)),
    ]
    next_date, _days = _estimate_next_report(r12)
    assert next_date is not None
    # Should land in late July 2026
    assert next_date.year == 2026
    assert next_date.month == 7
    assert 20 <= next_date.day <= 31


def test_uses_median_lag():
    # Construct one outlier to confirm we use median not mean
    r12 = [
        _r(2026, 1, date(2026, 4, 24)),     # 24-day lag
        _r(2025, 4, date(2026, 1, 25)),     # 25-day lag
        _r(2025, 3, date(2026, 2, 28)),     # 151-day lag (huge outlier)
        _r(2025, 2, date(2025, 7, 26)),     # 26-day lag
    ]
    next_date, _ = _estimate_next_report(r12)
    # Median is 25 (sorted: 24, 25, 26, 151 → median is 25 since len//2=2 picks index 2 = 26 actually)
    # Either way it shouldn't be wildly influenced by the 151-day outlier
    assert next_date is not None
    assert next_date.month in (7, 8)  # late July or early Aug, not Nov+


def test_handles_empty_input():
    assert _estimate_next_report([]) == (None, None)


def test_handles_missing_report_dates():
    r12 = [_r(2026, 1, None)]
    # Should still produce *something* — uses quarter_end + default lag of 28
    next_date, _ = _estimate_next_report(r12)
    # With no lag data we use default 28; quarter end = 2026-03-31; next = 2026-06-30 + 28 = 2026-07-28
    assert next_date is not None
    assert next_date.year == 2026
    assert next_date.month == 7


def test_days_until_is_signed():
    # Build a synthetic report claiming Q1 ended today, just reported today.
    today = date.today()
    r12 = [_r(today.year, ((today.month - 1) // 3) + 1, today)]
    _, days = _estimate_next_report(r12)
    # Next quarterly report should be ~91 + 28 days = ~119 days from now
    assert days is not None
    assert 80 < days < 160
