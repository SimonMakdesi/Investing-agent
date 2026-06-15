"""Derived fundamentals from Börsdata reports.

The Analyst gets a compact, readable summary — not raw report rows.
Goal: ~10–15 lines per company so the Analyst can reason about quality,
growth, profitability, leverage, and valuation without drowning in numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from src.data.borsdata import BorsdataClient, Report

log = logging.getLogger(__name__)


@dataclass
class Fundamentals:
    """Compact fundamentals view for one company, computed from R12 + annual reports."""
    # Latest R12 snapshot
    latest_revenue_msek: float
    latest_revenue_year: int
    latest_revenue_period: int
    latest_report_date: str | None
    currency: str  # report currency (SEK for Nordic, USD for US, …)

    revenue_growth_yoy_pct: float | None        # vs R12 four quarters back
    revenue_growth_3y_cagr_pct: float | None    # 3-year CAGR from annual reports
    gross_margin_pct: float | None
    operating_margin_pct: float | None
    net_margin_pct: float | None

    roe_pct: float | None                       # net income (R12) / equity
    net_debt_msek: float                        # negative = net cash
    net_debt_to_equity_pct: float | None
    free_cash_flow_msek: float
    fcf_yield_pct: float | None                 # FCF / market cap at current price

    dividend_per_share: float
    dividend_yield_pct: float | None            # div / current price
    pe_ttm: float | None                        # current price / trailing EPS
    earnings_per_share: float

    # Earnings-date estimate (Börsdata doesn't expose a calendar API, so we
    # extrapolate from the publication lag of recent reports).
    next_report_estimated_date: date | None
    next_report_days_until: int | None

    @property
    def has_data(self) -> bool:
        return self.latest_revenue_msek > 0


def compute(client: BorsdataClient, ins_id: int, current_price: float | None = None) -> Fundamentals | None:
    """Build the Fundamentals view for one instrument.

    `current_price` is the SEK-normalised price/share (the pipeline's accounting
    base). Reports are in the company's native currency, so for valuation ratios
    we convert the price back to native here — keeping P/E, dividend yield, and
    FCF yield currency-consistent for US names as well as Nordic. If omitted,
    those fields are None.
    """
    from src.data import fx  # local import keeps module load cheap / cycle-free
    try:
        r12 = client.get_r12_reports(ins_id, max_count=8)
        annual = client.get_year_reports(ins_id, max_count=4)
    except Exception as e:
        log.warning("Failed to fetch Börsdata reports for insId=%d: %s", ins_id, e)
        return None

    if not r12:
        log.warning("No R12 reports for insId=%d", ins_id)
        return None

    latest = r12[0]
    prior_yoy = r12[4] if len(r12) >= 5 else None  # 4 quarters back

    revenue_growth_yoy = None
    if prior_yoy and prior_yoy.revenues > 0:
        revenue_growth_yoy = (latest.revenues / prior_yoy.revenues - 1) * 100

    revenue_growth_3y_cagr = None
    if len(annual) >= 4 and annual[3].revenues > 0:
        # annual[0] is most recent, annual[3] is 3 years prior
        ratio = annual[0].revenues / annual[3].revenues
        if ratio > 0:
            revenue_growth_3y_cagr = (ratio ** (1 / 3) - 1) * 100

    gross_margin = _pct(latest.gross_income, latest.revenues)
    operating_margin = _pct(latest.operating_income, latest.revenues)
    net_margin = _pct(latest.profit_to_equity_holders, latest.revenues)
    roe = _pct(latest.profit_to_equity_holders, latest.total_equity)
    net_debt_to_equity = _pct(latest.net_debt, latest.total_equity)

    currency = (latest.currency or "SEK").upper()

    pe = None
    div_yield = None
    fcf_yield = None
    if current_price and current_price > 0:
        # Convert the SEK price back to the report's native currency so ratios
        # line up with EPS / dividend / FCF (which are native). For Nordic names
        # the rate is 1.0 and this is a no-op.
        fx_rate = fx.rate(currency) or 1.0
        price_native = current_price / fx_rate
        if latest.earnings_per_share > 0:
            pe = price_native / latest.earnings_per_share
        if latest.dividend > 0:
            div_yield = latest.dividend / price_native * 100
        market_cap_native = price_native * latest.number_of_shares
        if market_cap_native > 0 and latest.free_cash_flow:
            fcf_yield = latest.free_cash_flow / market_cap_native * 100

    next_date, days_until = _estimate_next_report(r12)

    return Fundamentals(
        latest_revenue_msek=latest.revenues,
        latest_revenue_year=latest.year,
        latest_revenue_period=latest.period,
        latest_report_date=latest.report_date.isoformat() if latest.report_date else None,
        currency=currency,
        revenue_growth_yoy_pct=revenue_growth_yoy,
        revenue_growth_3y_cagr_pct=revenue_growth_3y_cagr,
        gross_margin_pct=gross_margin,
        operating_margin_pct=operating_margin,
        net_margin_pct=net_margin,
        roe_pct=roe,
        net_debt_msek=latest.net_debt,
        net_debt_to_equity_pct=net_debt_to_equity,
        free_cash_flow_msek=latest.free_cash_flow,
        fcf_yield_pct=fcf_yield,
        dividend_per_share=latest.dividend,
        dividend_yield_pct=div_yield,
        pe_ttm=pe,
        earnings_per_share=latest.earnings_per_share,
        next_report_estimated_date=next_date,
        next_report_days_until=days_until,
    )


def _estimate_next_report(r12: list[Report]) -> tuple[date | None, int | None]:
    """Estimate when the next quarterly report will drop.

    Strategy:
    - Use the median publication lag (report_Date - report_End_Date) across
      the last few quarters. For most Nordic large caps this is 21-35 days.
    - The next quarter ends ~91 days after the latest one's report_End_Date.
    - Expected next publication = next quarter end + median lag.

    Returns (date, days_from_today). Either (None, None) if we can't estimate.
    """
    # We always need at least one report with a usable year/period to derive
    # the next quarter end. report_date is optional (falls back to default lag).
    quarters_with_q_end = [(r, _quarter_end(r)) for r in r12]
    quarters_with_q_end = [(r, q) for r, q in quarters_with_q_end if q is not None]
    if not quarters_with_q_end:
        return None, None

    latest_q_end = quarters_with_q_end[0][1]
    next_q_end = latest_q_end + timedelta(days=91)

    # Median publication lag in days from reports that have both q_end and report_date.
    lags = [
        (r.report_date - q).days
        for r, q in quarters_with_q_end[:4]
        if r.report_date is not None
    ]
    median_lag = sorted(lags)[len(lags) // 2] if lags else 28
    median_lag = max(7, min(60, median_lag))  # clamp to plausible range

    next_date = next_q_end + timedelta(days=median_lag)
    days_until = (next_date - date.today()).days
    return next_date, days_until


def _quarter_end(report: Report) -> date | None:
    """R12 rolling-12 reports include the end-of-period date as a parsable field
    on the raw record. We carry it via report_date only; here we approximate the
    most recent quarter end from year + period (period is 1-4 for the four
    quarters)."""
    if report.year <= 0 or report.period not in (1, 2, 3, 4):
        return None
    # Approximate end-of-quarter for the latest R12: assume non-broken fiscal year
    quarter_end_month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    m, d = quarter_end_month_day[report.period]
    try:
        return date(report.year, m, d)
    except ValueError:
        return None


def format_for_analyst(f: Fundamentals) -> str:
    """Compact text block for the Analyst's user message."""
    if not f.has_data:
        return "  (no fundamentals available from Börsdata)"

    def _v(x: float | None, suffix: str = "", fmt: str = "{:+.1f}") -> str:
        return f"{fmt.format(x)}{suffix}" if x is not None else "n/a"

    unit = f"M{f.currency}"  # e.g. MSEK, MUSD — amounts are in native-currency millions

    lines = [
        f"  Latest R12 (year {f.latest_revenue_year} Q{f.latest_revenue_period}, "
        f"reported {f.latest_report_date or '?'}, currency {f.currency})",
        f"  Revenue:           {f.latest_revenue_msek:>12,.0f} {unit}   "
        f"YoY: {_v(f.revenue_growth_yoy_pct, '%')}   "
        f"3y CAGR: {_v(f.revenue_growth_3y_cagr_pct, '%')}",
        f"  Margins:           gross {_v(f.gross_margin_pct, '%', '{:.1f}')}   "
        f"operating {_v(f.operating_margin_pct, '%', '{:.1f}')}   "
        f"net {_v(f.net_margin_pct, '%', '{:.1f}')}",
        f"  Profitability:     ROE {_v(f.roe_pct, '%', '{:.1f}')}   "
        f"EPS {f.earnings_per_share:.2f}   "
        f"FCF {f.free_cash_flow_msek:,.0f} {unit}   "
        f"FCF yield {_v(f.fcf_yield_pct, '%', '{:.1f}')}",
        f"  Balance sheet:     net debt {f.net_debt_msek:,.0f} {unit}"
        + (" (NET CASH)" if f.net_debt_msek < 0 else "")
        + f"   net debt / equity {_v(f.net_debt_to_equity_pct, '%', '{:.1f}')}",
        f"  Valuation:         P/E {_v(f.pe_ttm, '', '{:.1f}')}   "
        f"div/share {f.dividend_per_share:.2f}   "
        f"div yield {_v(f.dividend_yield_pct, '%', '{:.2f}')}",
    ]
    if f.next_report_estimated_date is not None and f.next_report_days_until is not None:
        urgency = ""
        if f.next_report_days_until <= 14:
            urgency = "  <-- IMMINENT"
        elif f.next_report_days_until <= 30:
            urgency = "  <-- near-term"
        lines.append(
            f"  Next earnings:     est. ~{f.next_report_estimated_date.isoformat()} "
            f"(~{f.next_report_days_until:+d} days from today){urgency}"
        )
    return "\n".join(lines)


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator * 100
