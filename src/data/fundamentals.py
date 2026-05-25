"""Derived fundamentals from Börsdata reports.

The Analyst gets a compact, readable summary — not raw report rows.
Goal: ~10–15 lines per company so the Analyst can reason about quality,
growth, profitability, leverage, and valuation without drowning in numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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

    @property
    def has_data(self) -> bool:
        return self.latest_revenue_msek > 0


def compute(client: BorsdataClient, ins_id: int, current_price: float | None = None) -> Fundamentals | None:
    """Build the Fundamentals view for one instrument.

    `current_price` (SEK/share) is required for P/E, dividend yield, FCF yield.
    If omitted, those fields are None.
    """
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

    pe = None
    div_yield = None
    fcf_yield = None
    if current_price and current_price > 0:
        if latest.earnings_per_share > 0:
            pe = current_price / latest.earnings_per_share
        if latest.dividend > 0:
            div_yield = latest.dividend / current_price * 100
        market_cap_msek = current_price * latest.number_of_shares
        if market_cap_msek > 0 and latest.free_cash_flow:
            fcf_yield = latest.free_cash_flow / market_cap_msek * 100

    return Fundamentals(
        latest_revenue_msek=latest.revenues,
        latest_revenue_year=latest.year,
        latest_revenue_period=latest.period,
        latest_report_date=latest.report_date.isoformat() if latest.report_date else None,
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
    )


def format_for_analyst(f: Fundamentals) -> str:
    """Compact text block for the Analyst's user message."""
    if not f.has_data:
        return "  (no fundamentals available from Börsdata)"

    def _v(x: float | None, suffix: str = "", fmt: str = "{:+.1f}") -> str:
        return f"{fmt.format(x)}{suffix}" if x is not None else "n/a"

    lines = [
        f"  Latest R12 (year {f.latest_revenue_year} Q{f.latest_revenue_period}, "
        f"reported {f.latest_report_date or '?'})",
        f"  Revenue:           {f.latest_revenue_msek:>12,.0f} MSEK   "
        f"YoY: {_v(f.revenue_growth_yoy_pct, '%')}   "
        f"3y CAGR: {_v(f.revenue_growth_3y_cagr_pct, '%')}",
        f"  Margins:           gross {_v(f.gross_margin_pct, '%', '{:.1f}')}   "
        f"operating {_v(f.operating_margin_pct, '%', '{:.1f}')}   "
        f"net {_v(f.net_margin_pct, '%', '{:.1f}')}",
        f"  Profitability:     ROE {_v(f.roe_pct, '%', '{:.1f}')}   "
        f"EPS {f.earnings_per_share:.2f}   "
        f"FCF {f.free_cash_flow_msek:,.0f} MSEK   "
        f"FCF yield {_v(f.fcf_yield_pct, '%', '{:.1f}')}",
        f"  Balance sheet:     net debt {f.net_debt_msek:,.0f} MSEK"
        + (" (NET CASH)" if f.net_debt_msek < 0 else "")
        + f"   net debt / equity {_v(f.net_debt_to_equity_pct, '%', '{:.1f}')}",
        f"  Valuation:         P/E {_v(f.pe_ttm, '', '{:.1f}')}   "
        f"div/share {f.dividend_per_share:.2f}   "
        f"div yield {_v(f.dividend_yield_pct, '%', '{:.2f}')}",
    ]
    return "\n".join(lines)


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator * 100
