"""Finansinspektionen insider transactions (insynsregister).

FI publishes the registry as a daily CSV. The CSV is semicolon-delimited,
encoded as UTF-16 LE with BOM, in Swedish. We parse it into typed records.

Endpoint format (subject to change by FI):
    https://marknadssok.fi.se/publiceringsklient/sv-SE/Search/Search?
    SearchFunctionType=Insyn&Utgivare=&PersonILedandeStallningNamn=&Transaktionstyp=
    &Publiceringsdatum.From=YYYY-MM-DD&Publiceringsdatum.To=YYYY-MM-DD&button=export
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import requests

log = logging.getLogger(__name__)

FI_EXPORT_URL = "https://marknadssok.fi.se/publiceringsklient/sv-SE/Search/Search"
DEFAULT_MIN_VALUE_SEK = 500_000
USER_AGENT = "investing-agent/0.1 (personal research)"


@dataclass(frozen=True)
class InsiderTransaction:
    publication_date: date
    issuer: str
    person: str
    position: str
    insider_status: str
    transaction_type: str  # e.g. "Förvärv" (buy), "Avyttring" (sell)
    instrument_name: str
    isin: str
    transaction_date: date | None
    volume: float
    unit: str  # usually "Antal" (number of shares)
    price: float
    currency: str
    total_value_sek: float

    @property
    def is_buy(self) -> bool:
        return self.transaction_type.lower().startswith("förvärv")

    @property
    def is_sell(self) -> bool:
        return self.transaction_type.lower().startswith("avytt")


def _parse_swedish_decimal(s: str) -> float:
    """Swedish numbers use ',' as decimal separator and ' ' as thousand separator."""
    if not s:
        return 0.0
    cleaned = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    # FI uses YYYY-MM-DD
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def fetch_recent(days_back: int = 7) -> list[InsiderTransaction]:
    """All insider transactions published in the last `days_back` days."""
    today = date.today()
    params = {
        "SearchFunctionType": "Insyn",
        "Utgivare": "",
        "PersonILedandeStallningNamn": "",
        "Transaktionstyp": "",
        "Publiceringsdatum.From": (today - timedelta(days=days_back)).isoformat(),
        "Publiceringsdatum.To": today.isoformat(),
        "button": "export",
    }
    headers = {"User-Agent": USER_AGENT}

    log.info("Fetching FI insider transactions for last %d days", days_back)
    resp = requests.get(FI_EXPORT_URL, params=params, headers=headers, timeout=60)
    resp.raise_for_status()

    # FI ships UTF-16 LE — sometimes with BOM, sometimes without. Detect both.
    raw = resp.content
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif b"\x00" in raw[:32]:
        # Interleaved nulls in the first chunk => UTF-16 with no BOM. LE is the FI default.
        text = raw.decode("utf-16-le")
    else:
        for enc in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise RuntimeError("Could not decode FI insider CSV response")

    # pandas handles embedded newlines / inconsistent quoting better than csv.DictReader.
    try:
        df = pd.read_csv(io.StringIO(text), sep=";", dtype=str, keep_default_na=False)
    except Exception as e:
        log.error("Failed to parse FI CSV: %s", e)
        return []

    out: list[InsiderTransaction] = []
    for row in df.to_dict(orient="records"):
        try:
            volume = _parse_swedish_decimal(row.get("Volym", ""))
            price = _parse_swedish_decimal(row.get("Pris", ""))
            currency = (row.get("Valuta", "SEK") or "SEK").strip()
            # For non-SEK currencies, total_value_sek is approximate (no FX conversion yet).
            total_value = volume * price
            tx = InsiderTransaction(
                publication_date=_parse_date(row.get("Publiceringsdatum", "")) or date.today(),
                issuer=(row.get("Emittent", "") or "").strip(),
                person=(row.get("Person i ledande ställning", "") or "").strip(),
                position=(row.get("Befattning", "") or "").strip(),
                insider_status=(row.get("Status", "") or "").strip(),
                transaction_type=(row.get("Karaktär", "") or "").strip(),
                instrument_name=(row.get("Instrumentnamn", "") or "").strip(),
                isin=(row.get("ISIN", "") or "").strip(),
                transaction_date=_parse_date(row.get("Transaktionsdatum", "")),
                volume=volume,
                unit=(row.get("Volymsenhet", "") or "Antal").strip(),
                price=price,
                currency=currency,
                total_value_sek=total_value,
            )
            out.append(tx)
        except Exception as e:
            log.warning("Skipped malformed FI row: %s", e)
            continue

    log.info("Fetched %d insider transactions", len(out))
    return out


def filter_significant_buys(
    transactions: list[InsiderTransaction],
    min_value_sek: float = DEFAULT_MIN_VALUE_SEK,
) -> list[InsiderTransaction]:
    """Buys above the threshold. Default 500k SEK matches the brief."""
    return [t for t in transactions if t.is_buy and t.total_value_sek >= min_value_sek]
