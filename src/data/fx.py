"""FX conversion to SEK, the portfolio's accounting base.

US (and other non-SEK) holdings are valued and executed in SEK by converting at
the live spot rate *at the boundary* — so the entire SEK-denominated core
(portfolio, risk caps, reporting) keeps working unchanged. A holding's SEK P&L
then correctly includes the currency move, which is the right view for a SEK
investor.

Rates are cached per process run (the cycle is short-lived). yfinance exposes
spot FX as `<CCY>SEK=X` (e.g. `USDSEK=X`).
"""

from __future__ import annotations

import logging

import yfinance as yf

log = logging.getLogger(__name__)

# Last-resort approximations if the live fetch fails. Deliberately rough and
# logged loudly — a stale rate is better than crashing a cycle, but we want to
# know it happened.
_FALLBACK_RATES: dict[str, float] = {
    "SEK": 1.0,
    "USD": 10.5,
    "EUR": 11.3,
    "NOK": 1.0,
    "DKK": 1.5,
    "GBP": 13.2,
}

_cache: dict[str, float] = {"SEK": 1.0}


def rate(currency: str) -> float:
    """SEK per 1 unit of `currency`. `rate('USD')` ≈ 10.5. SEK -> 1.0."""
    ccy = (currency or "SEK").upper()
    if ccy in _cache:
        return _cache[ccy]

    pair = f"{ccy}SEK=X"
    try:
        hist = yf.Ticker(pair).history(period="5d", auto_adjust=False)
        if not hist.empty and "Close" in hist.columns:
            r = float(hist["Close"].dropna().iloc[-1])
            if r > 0:
                _cache[ccy] = r
                log.info("FX %s->SEK = %.4f", ccy, r)
                return r
        log.warning("FX fetch for %s returned no data", pair)
    except Exception as e:
        log.warning("FX fetch for %s failed: %s", pair, e)

    fallback = _FALLBACK_RATES.get(ccy)
    if fallback is None:
        log.error("No FX rate or fallback for %s — treating as 1:1 SEK (LIKELY WRONG)", ccy)
        fallback = 1.0
    log.warning("Using FALLBACK FX %s->SEK = %.4f", ccy, fallback)
    _cache[ccy] = fallback
    return fallback


def to_sek(amount: float, currency: str) -> float:
    """Convert `amount` in `currency` to SEK."""
    if amount is None:
        return amount
    return amount * rate(currency)
