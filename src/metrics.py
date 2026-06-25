"""Compressed metrics per ticker.

The Screener should never see raw price history. It sees this struct —
one line of signals per name — and decides what's worth a deeper look.

Designed to fit on one line in the Screener prompt so 150 names cost
~150 tokens of input, not ~150,000.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.data.insiders import InsiderTransaction
from src.data.prices import get_history

log = logging.getLogger(__name__)


@dataclass
class TickerMetrics:
    ticker: str
    as_of: date
    last_close: float
    return_1m_pct: float          # 21 trading days
    return_3m_pct: float          # 63 trading days
    return_12m_pct: float
    distance_from_200ma_pct: float  # +5.2 means 5.2% above 200-day SMA
    distance_from_52w_high_pct: float  # -8.0 means 8% below 52w high
    vol_30d_annualized_pct: float
    insider_buy_value_30d_sek: float  # sum of recent insider buys on this ticker
    insider_buy_count_30d: int
    avg_turnover_30d: float = 0.0  # avg daily close*volume over 30d, NATIVE currency

    def one_liner(self) -> str:
        """Compact form for the Scout prompt: ~one line of text."""
        return (
            f"{self.ticker:12s}  "
            f"px={self.last_close:>8.2f}  "
            f"1m={self.return_1m_pct:+6.1f}%  "
            f"3m={self.return_3m_pct:+6.1f}%  "
            f"12m={self.return_12m_pct:+7.1f}%  "
            f"vs200ma={self.distance_from_200ma_pct:+6.1f}%  "
            f"vs52wH={self.distance_from_52w_high_pct:+6.1f}%  "
            f"vol30d={self.vol_30d_annualized_pct:5.1f}%  "
            f"turn30d={self.avg_turnover_30d/1e6:>6.1f}M  "
            f"insider30d={self.insider_buy_value_30d_sek/1000:>6.0f}k×{self.insider_buy_count_30d}"
        )


def _pct_return(prices: pd.Series, lookback_days: int) -> float:
    if len(prices) <= lookback_days:
        return 0.0
    past = prices.iloc[-lookback_days - 1]
    now = prices.iloc[-1]
    if past <= 0:
        return 0.0
    return float((now / past - 1.0) * 100.0)


def compute(
    ticker: str,
    insiders_for_ticker: list[InsiderTransaction] | None = None,
) -> TickerMetrics | None:
    """Build the metrics row for one ticker. Returns None if price history is unavailable."""
    history = get_history(ticker, days=400)
    if history.empty or "Close" not in history.columns:
        log.warning("No history for %s — skipping metrics", ticker)
        return None

    close = history["Close"].dropna()
    if len(close) < 30:
        log.warning("Insufficient history for %s (%d rows) — skipping", ticker, len(close))
        return None

    last_close = float(close.iloc[-1])
    sma200 = float(close.rolling(window=200, min_periods=50).mean().iloc[-1])
    distance_from_200ma = (last_close / sma200 - 1.0) * 100.0 if sma200 > 0 else 0.0

    high_52w = float(close.tail(252).max()) if len(close) >= 20 else last_close
    distance_from_52w_high = (last_close / high_52w - 1.0) * 100.0 if high_52w > 0 else 0.0

    daily_returns = close.pct_change().dropna()
    vol_30d = float(daily_returns.tail(30).std() * np.sqrt(252) * 100.0) if len(daily_returns) >= 30 else 0.0

    # Average daily turnover (close*volume) over the last 30 days, in the stock's
    # native currency. Used as a liquidity floor to drop untradeable micro-caps.
    avg_turnover = 0.0
    if "Volume" in history.columns:
        vol = history["Volume"].astype(float)
        turnover = (close * vol).dropna()
        if len(turnover) > 0:
            avg_turnover = float(turnover.tail(30).mean())

    # Insider buys aggregated over the last 30 calendar days
    insider_value = 0.0
    insider_count = 0
    if insiders_for_ticker:
        cutoff = date.today() - timedelta(days=30)
        for tx in insiders_for_ticker:
            if tx.is_buy and tx.publication_date >= cutoff:
                insider_value += tx.total_value_sek
                insider_count += 1

    return TickerMetrics(
        ticker=ticker,
        as_of=close.index[-1].date() if hasattr(close.index[-1], "date") else date.today(),
        last_close=last_close,
        return_1m_pct=_pct_return(close, 21),
        return_3m_pct=_pct_return(close, 63),
        return_12m_pct=_pct_return(close, 252),
        distance_from_200ma_pct=distance_from_200ma,
        distance_from_52w_high_pct=distance_from_52w_high,
        vol_30d_annualized_pct=vol_30d,
        insider_buy_value_30d_sek=insider_value,
        insider_buy_count_30d=insider_count,
        avg_turnover_30d=avg_turnover,
    )


def index_insiders_by_issuer(
    transactions: list[InsiderTransaction],
) -> dict[str, list[InsiderTransaction]]:
    """Group insider transactions by raw issuer name. Use src.issuer_match for
    ticker-level lookups (the issuer name does not equal our short universe name)."""
    out: dict[str, list[InsiderTransaction]] = {}
    for tx in transactions:
        out.setdefault(tx.issuer, []).append(tx)
    return out


def to_dict(m: TickerMetrics) -> dict:
    """For JSON serialization (e.g. when archiving a screener input)."""
    d = asdict(m)
    d["as_of"] = m.as_of.isoformat()
    return d
