"""Blended OMXS30 + S&P 500 benchmark.

Once the book holds US names, OMXS30 alone is an unfair yardstick. We blend it
with the S&P 500 by the portfolio's actual regional equity weight. The S&P side
is measured **in SEK** (index × USDSEK) so the benchmark reflects what a SEK
investor actually earns, FX move included.

The blended series is returned as a normalised index (first available day = the
common base), so existing dashboard code that does `bm_now / bm_start` keeps
working unchanged.
"""

from __future__ import annotations

import logging
from datetime import date

import yfinance as yf

log = logging.getLogger(__name__)

OMX_TICKER = "^OMX"      # OMX Stockholm 30 (SEK)
SP500_TICKER = "^GSPC"   # S&P 500 (USD)
USDSEK_TICKER = "USDSEK=X"


def _close_hist(ticker: str, period: str = "2y") -> dict[date, float]:
    """Date -> close for a ticker, flattening yfinance's multi-index."""
    df = yf.download(ticker, period=period, progress=False, auto_adjust=False)
    if df is None or df.empty:
        log.warning("Benchmark fetch returned no data for %s", ticker)
        return {}
    if hasattr(df.columns, "get_level_values"):
        try:
            close = df["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
        except KeyError:
            close = df.iloc[:, 0]
    else:
        close = df["Close"]
    return {ts.date(): float(p) for ts, p in close.dropna().items()}


def regional_weights(portfolio, prices: dict[str, float]) -> tuple[float, float]:
    """(se_weight, us_weight) from current equity SEK value by holding currency.

    Region is read from each Holding.currency (USD -> US, else Swedish/Nordic).
    Falls back to 50/50 when there is no equity to weight by.
    """
    se_val = us_val = 0.0
    for h in portfolio.holdings.values():
        val = h.shares * prices.get(h.ticker, h.avg_cost)
        if (h.currency or "SEK").upper() == "USD":
            us_val += val
        else:
            se_val += val
    total = se_val + us_val
    if total <= 0:
        return 0.5, 0.5
    return se_val / total, us_val / total


def blended_index_hist(se_weight: float, us_weight: float, period: str = "2y") -> dict[date, float]:
    """Normalised blended index keyed by date (first common day = base 1.0).

    Each component is normalised to the first day both are available, then blended
    by weight. The S&P 500 is converted to SEK via USDSEK before normalising.
    """
    omx = _close_hist(OMX_TICKER, period)
    sp = _close_hist(SP500_TICKER, period)
    fx = _close_hist(USDSEK_TICKER, period)
    if not omx:
        return {}
    if not sp or not fx:
        log.info("S&P/FX history unavailable — benchmark falls back to OMXS30 only")
        base = omx[min(omx)]
        return {d: v / base for d, v in omx.items()} if base else {}

    sp_sek = {d: sp[d] * fx[d] for d in sp.keys() & fx.keys()}
    common = sorted(omx.keys() & sp_sek.keys())
    if not common:
        return {}
    d0 = common[0]
    omx0, sp0 = omx[d0], sp_sek[d0]
    out: dict[date, float] = {}
    for d in common:
        omx_norm = omx[d] / omx0 if omx0 else 1.0
        sp_norm = sp_sek[d] / sp0 if sp0 else 1.0
        out[d] = se_weight * omx_norm + us_weight * sp_norm
    return out


def blended_daily_change(se_weight: float, us_weight: float) -> tuple[str, float | None]:
    """(label, blended daily % change). S&P measured in SEK terms.

    Returns the label describing the blend and the weighted daily change, or
    (label, None) if data is unavailable.
    """
    label = f"{se_weight*100:.0f}% OMXS30 / {us_weight*100:.0f}% S&P 500"

    def _daily_pct(hist: dict[date, float]) -> float | None:
        if len(hist) < 2:
            return None
        ds = sorted(hist)
        last, prev = hist[ds[-1]], hist[ds[-2]]
        return (last / prev - 1.0) * 100.0 if prev else None

    omx = _close_hist(OMX_TICKER, "10d")
    omx_pct = _daily_pct(omx)
    sp = _close_hist(SP500_TICKER, "10d")
    fx = _close_hist(USDSEK_TICKER, "10d")
    sp_sek = {d: sp[d] * fx[d] for d in sp.keys() & fx.keys()} if sp and fx else {}
    sp_pct = _daily_pct(sp_sek)

    if omx_pct is None and sp_pct is None:
        return label, None
    if sp_pct is None:
        return "OMXS30", omx_pct
    if omx_pct is None:
        return "S&P 500 (SEK)", sp_pct
    return label, se_weight * omx_pct + us_weight * sp_pct
