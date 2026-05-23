"""Paper portfolio: load, save, buy, sell, value.

Holdings are stored in `state/portfolio.json` as the source of truth.
Every mutation also appends a JSON-line entry to `state/transactions.log`.

Sleeve labels (Core / Aggressive) are recorded per-holding so risk
checks can enforce the sleeve caps separately.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import PORTFOLIO_FILE, STOCKHOLM_TZ, TRANSACTIONS_LOG

log = logging.getLogger(__name__)


class Sleeve(str, Enum):
    CORE = "core"
    AGGRESSIVE = "aggressive"


class Holding(BaseModel):
    ticker: str
    shares: float
    avg_cost: float  # SEK per share, cost basis
    sleeve: Sleeve
    opened_at: datetime  # first buy that established the position
    sector: str | None = None  # optional, filled in when known


class Portfolio(BaseModel):
    cash_sek: float
    holdings: dict[str, Holding] = Field(default_factory=dict)
    inception_date: datetime
    initial_capital_sek: float

    @classmethod
    def load(cls, path: Path = PORTFOLIO_FILE) -> "Portfolio":
        if not path.exists():
            raise FileNotFoundError(
                f"{path} does not exist. Run scripts/init_portfolio.py first."
            )
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path = PORTFOLIO_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def value(self, prices: dict[str, float]) -> float:
        """Total portfolio value at the given prices. Missing prices => uses avg_cost."""
        equity = sum(
            h.shares * prices.get(h.ticker, h.avg_cost) for h in self.holdings.values()
        )
        return self.cash_sek + equity

    def equity_value(self, prices: dict[str, float]) -> float:
        return sum(h.shares * prices.get(h.ticker, h.avg_cost) for h in self.holdings.values())

    def buy(
        self,
        ticker: str,
        shares: float,
        price: float,
        sleeve: Sleeve,
        sector: str | None = None,
        rationale: str = "",
    ) -> None:
        if shares <= 0 or price <= 0:
            raise ValueError("shares and price must be positive")
        cost = shares * price
        if cost > self.cash_sek + 1e-6:
            raise ValueError(
                f"Insufficient cash: need {cost:.2f} SEK, have {self.cash_sek:.2f} SEK"
            )

        now = datetime.now(tz=STOCKHOLM_TZ)
        existing = self.holdings.get(ticker)
        if existing:
            if existing.sleeve != sleeve:
                raise ValueError(
                    f"Sleeve mismatch for {ticker}: existing={existing.sleeve}, new={sleeve}. "
                    "Resolve sleeve label before adding."
                )
            total_shares = existing.shares + shares
            total_cost = existing.shares * existing.avg_cost + cost
            existing.shares = total_shares
            existing.avg_cost = total_cost / total_shares
            if sector and not existing.sector:
                existing.sector = sector
        else:
            self.holdings[ticker] = Holding(
                ticker=ticker,
                shares=shares,
                avg_cost=price,
                sleeve=sleeve,
                opened_at=now,
                sector=sector,
            )

        self.cash_sek -= cost
        _log_transaction(
            {
                "ts": now.isoformat(),
                "action": "buy",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "cost_sek": cost,
                "sleeve": sleeve.value,
                "cash_after": self.cash_sek,
                "rationale": rationale,
            }
        )

    def sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        rationale: str = "",
    ) -> None:
        if shares <= 0 or price <= 0:
            raise ValueError("shares and price must be positive")
        holding = self.holdings.get(ticker)
        if not holding:
            raise ValueError(f"Not held: {ticker}")
        if shares > holding.shares + 1e-9:
            raise ValueError(
                f"Cannot sell {shares} of {ticker}; only hold {holding.shares}"
            )

        proceeds = shares * price
        realized_pnl = (price - holding.avg_cost) * shares
        now = datetime.now(tz=STOCKHOLM_TZ)

        holding.shares -= shares
        if holding.shares < 1e-9:
            del self.holdings[ticker]
        self.cash_sek += proceeds

        _log_transaction(
            {
                "ts": now.isoformat(),
                "action": "sell",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "proceeds_sek": proceeds,
                "realized_pnl_sek": realized_pnl,
                "cash_after": self.cash_sek,
                "rationale": rationale,
            }
        )


def _log_transaction(entry: dict) -> None:
    TRANSACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRANSACTIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
