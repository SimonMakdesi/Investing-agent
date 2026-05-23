"""Price data via Yahoo Finance.

Swedish tickers on Yahoo use the `.ST` suffix (e.g. VOLV-B.ST, ERIC-B.ST).
Investmentbolag / B-share tickers use a dash, not a dot (VOLV-B.ST, not VOLV.B.ST).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSnapshot:
    ticker: str
    as_of: date
    close: float
    currency: str


def get_latest_close(ticker: str) -> PriceSnapshot | None:
    """Latest available daily close. Returns None on failure."""
    try:
        t = yf.Ticker(ticker)
        # 5d window handles weekends/holidays without pulling a year of history.
        hist = t.history(period="5d", auto_adjust=False)
        if hist.empty:
            log.warning("No price history returned for %s", ticker)
            return None
        last = hist.iloc[-1]
        last_date = hist.index[-1].date()
        currency = t.fast_info.get("currency", "SEK") if hasattr(t, "fast_info") else "SEK"
        return PriceSnapshot(
            ticker=ticker,
            as_of=last_date,
            close=float(last["Close"]),
            currency=currency,
        )
    except Exception as e:
        log.warning("Failed to fetch price for %s: %s", ticker, e)
        return None


def get_history(ticker: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for the last `days` calendar days. Empty DataFrame on failure."""
    try:
        start = datetime.now().date() - timedelta(days=days)
        df = yf.download(ticker, start=start, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame()
        # yfinance returns a column MultiIndex when downloading; flatten.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.warning("Failed to fetch history for %s: %s", ticker, e)
        return pd.DataFrame()


def get_latest_closes(tickers: list[str]) -> dict[str, PriceSnapshot]:
    """Batch helper. Returns only tickers that succeeded."""
    out: dict[str, PriceSnapshot] = {}
    for t in tickers:
        snap = get_latest_close(t)
        if snap is not None:
            out[t] = snap
    return out
