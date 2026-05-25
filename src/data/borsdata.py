"""Börsdata API client (Pro+ tier required).

Wraps the subset of endpoints the agent uses:
- /instruments — full Nordic instrument list (cached for the run)
- /instruments/{insId}/reports/r12 — rolling-12-month financials
- /instruments/{insId}/reports/year — annual financials
- /holdings/insider — insider transactions (with the all-important
  `equityProgram` flag so we can filter out option/grant noise)

Rate limit: 100 calls per 10s, 10k per 24h. Our weekly cycle uses ~10 calls;
daily cycle uses ~3. Well below limits.

Auth: API key as `?authKey=...` query parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from typing import Any

import requests

from src.config import settings

log = logging.getLogger(__name__)

BASE_URL = "https://apiservice.borsdata.se/v1"
DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class Instrument:
    ins_id: int
    name: str
    yahoo_ticker: str
    isin: str
    sector_id: int
    market_id: int
    branch_id: int
    country_id: int
    currency: str


@dataclass(frozen=True)
class Report:
    """One financial report row (R12 or annual). All amounts in report currency
    (typically SEK or MSEK depending on company — see `currency_ratio` to normalize)."""
    year: int
    period: int
    report_date: date | None
    revenues: float
    gross_income: float
    operating_income: float
    profit_before_tax: float
    profit_to_equity_holders: float
    earnings_per_share: float
    number_of_shares: float  # in millions per Börsdata convention
    dividend: float
    total_equity: float
    net_debt: float
    free_cash_flow: float
    cash_flow_from_operating: float
    total_assets: float
    stock_price_average: float
    currency: str
    currency_ratio: float


@dataclass(frozen=True)
class InsiderTx:
    """One insider transaction.

    Direction is read from the SIGN of `amount_sek` (positive = buy,
    negative = sell) — Börsdata uses many transaction_type codes
    (19, 25, 18, 3, 0, …) which are not consistent enough to use for direction.

    `equity_program=True` means option/grant mechanics. Always exclude those
    from conviction signals — that's the bug the agent flagged on SOBI/AAK.
    """
    ins_id: int
    owner_name: str
    owner_position: str | None
    equity_program: bool
    shares: float
    price: float
    amount_sek: float  # SIGNED: positive = buy, negative = sell
    transaction_type: int  # opaque Börsdata code; do not use for direction
    transaction_date: date | None
    verification_date: date | None

    @property
    def is_buy(self) -> bool:
        return self.amount_sek > 0

    @property
    def is_sell(self) -> bool:
        return self.amount_sek < 0

    @property
    def is_conviction(self) -> bool:
        """A discretionary (non-program) transaction with real money flow.

        Filters out:
          - equity-program transactions (option grants/exercises)
          - transactions with implausible per-share price (>100k SEK/share is
            never a real Swedish stock — these are bond/warrant subscriptions
            where Börsdata mis-reports price as total value)
          - transactions with impossibly large total amounts (>50bn SEK from
            a single insider is a data error, not a real signal)
        """
        if self.equity_program or self.amount_sek == 0:
            return False
        if self.price > 100_000 or self.price < 0:
            return False
        if abs(self.amount_sek) > 50_000_000_000:
            return False
        return True


class BorsdataError(RuntimeError):
    pass


def _parse_dt(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date()
    except (ValueError, TypeError):
        return None


class BorsdataClient:
    """Thin wrapper around the Börsdata REST API. One instance per pipeline run."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.borsdata_api_key
        if not self.api_key:
            raise BorsdataError("BORSDATA_API_KEY missing")
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        full_params = {**(params or {}), "authKey": self.api_key}
        url = f"{BASE_URL}{path}"
        r = self.session.get(url, params=full_params, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            raise BorsdataError(
                f"GET {path} -> {r.status_code}: {r.text[:200]}"
            )
        return r.json()

    @cached_property
    def instruments(self) -> list[Instrument]:
        log.info("Fetching Börsdata instruments ...")
        data = self._get("/instruments")
        out = []
        for raw in data.get("instruments", []):
            out.append(Instrument(
                ins_id=raw["insId"],
                name=raw.get("name", ""),
                yahoo_ticker=raw.get("yahoo", "") or "",
                isin=raw.get("isin", "") or "",
                sector_id=raw.get("sectorId", 0) or 0,
                market_id=raw.get("marketId", 0) or 0,
                branch_id=raw.get("branchId", 0) or 0,
                country_id=raw.get("countryId", 0) or 0,
                currency=raw.get("stockPriceCurrency", "SEK") or "SEK",
            ))
        log.info("Loaded %d Börsdata instruments", len(out))
        return out

    @cached_property
    def yahoo_to_ins_id(self) -> dict[str, int]:
        """Map our Yahoo .ST tickers to Börsdata insIds."""
        return {i.yahoo_ticker: i.ins_id for i in self.instruments if i.yahoo_ticker}

    def get_r12_reports(self, ins_id: int, max_count: int = 8) -> list[Report]:
        """Latest N rolling-12-month reports (most recent first)."""
        data = self._get(f"/instruments/{ins_id}/reports/r12", {"maxCount": max_count})
        return [self._parse_report(r) for r in data.get("reports", [])]

    def get_year_reports(self, ins_id: int, max_count: int = 5) -> list[Report]:
        """Latest N annual reports."""
        data = self._get(f"/instruments/{ins_id}/reports/year", {"maxCount": max_count})
        return [self._parse_report(r) for r in data.get("reports", [])]

    def get_insider_transactions(self, ins_ids: list[int]) -> dict[int, list[InsiderTx]]:
        """All insider transactions for the listed instruments (Börsdata caps the
        history depth itself — typically ~year of data per instrument).

        Returns dict: ins_id -> list of transactions (raw, unfiltered).
        Caller filters by date / equity_program / transaction_type.
        """
        if not ins_ids:
            return {}
        # Börsdata caps at 50 insIds per call; chunk if necessary.
        out: dict[int, list[InsiderTx]] = {}
        for i in range(0, len(ins_ids), 50):
            chunk = ins_ids[i : i + 50]
            inst_list_str = ",".join(str(x) for x in chunk)
            data = self._get("/holdings/insider", {"instList": inst_list_str})
            for entry in data.get("list", []):
                ins_id = entry["insId"]
                out[ins_id] = [self._parse_insider(t, ins_id) for t in entry.get("values", [])]
        return out

    @staticmethod
    def _parse_report(raw: dict) -> Report:
        return Report(
            year=raw.get("year", 0) or 0,
            period=raw.get("period", 0) or 0,
            report_date=_parse_dt(raw.get("report_Date")),
            revenues=float(raw.get("revenues") or 0),
            gross_income=float(raw.get("gross_Income") or 0),
            operating_income=float(raw.get("operating_Income") or 0),
            profit_before_tax=float(raw.get("profit_Before_Tax") or 0),
            profit_to_equity_holders=float(raw.get("profit_To_Equity_Holders") or 0),
            earnings_per_share=float(raw.get("earnings_Per_Share") or 0),
            number_of_shares=float(raw.get("number_Of_Shares") or 0),
            dividend=float(raw.get("dividend") or 0),
            total_equity=float(raw.get("total_Equity") or 0),
            net_debt=float(raw.get("net_Debt") or 0),
            free_cash_flow=float(raw.get("free_Cash_Flow") or 0),
            cash_flow_from_operating=float(raw.get("cash_Flow_From_Operating_Activities") or 0),
            total_assets=float(raw.get("total_Assets") or 0),
            stock_price_average=float(raw.get("stock_Price_Average") or 0),
            currency=raw.get("currency", "SEK") or "SEK",
            currency_ratio=float(raw.get("currency_Ratio") or 1.0),
        )

    @staticmethod
    def _parse_insider(raw: dict, ins_id: int) -> InsiderTx:
        return InsiderTx(
            ins_id=ins_id,
            owner_name=raw.get("ownerName", "") or "",
            owner_position=raw.get("ownerPosition"),
            equity_program=bool(raw.get("equityProgram", False)),
            shares=float(raw.get("shares") or 0),
            price=float(raw.get("price") or 0),
            amount_sek=float(raw.get("amount") or 0),
            transaction_type=int(raw.get("transactionType", -1)),
            transaction_date=_parse_dt(raw.get("transactionDate")),
            verification_date=_parse_dt(raw.get("verificationDate")),
        )
