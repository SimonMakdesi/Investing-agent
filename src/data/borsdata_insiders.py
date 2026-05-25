"""Clean insider data via Börsdata.

Replaces the FI-scraping path for the screener and analyst inputs.
Critical improvement: Börsdata's `equityProgram` flag lets us exclude
option-grant mechanics that previously polluted "insider buy" signals
(the bug the agent itself caught on SOBI, AAK, BILL).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from src.data.borsdata import BorsdataClient, InsiderTx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsiderSummary:
    """Per-ticker insider summary computed over a window (default 90 days).

    Conviction transactions only: discretionary buys/sells, equity-program
    transactions excluded.
    """
    ins_id: int
    net_value_sek: float           # net SEK (positive = net buying, negative = net selling)
    gross_buy_value_sek: float     # sum of conviction buys
    gross_sell_value_sek: float    # sum of conviction sells
    distinct_buyers: int           # how many distinct insiders bought
    distinct_sellers: int          # how many distinct insiders sold
    buy_count: int
    sell_count: int
    largest_single_buy_sek: float
    largest_single_buy_owner: str
    transactions: list[InsiderTx]  # the underlying conviction transactions in the window

    @property
    def is_meaningful_buy_signal(self) -> bool:
        """Useful heuristic: net buying with at least one substantial single buyer."""
        return self.net_value_sek > 0 and self.largest_single_buy_sek >= 500_000


def summarize(transactions: list[InsiderTx], window_days: int = 90) -> InsiderSummary | None:
    """Reduce a ticker's raw transaction list to a single summary over the window.

    Filters out equityProgram transactions automatically — those should never be
    treated as conviction signals.
    """
    if not transactions:
        return None
    ins_id = transactions[0].ins_id
    cutoff = date.today() - timedelta(days=window_days)
    in_window = [
        t for t in transactions
        if t.transaction_date and t.transaction_date >= cutoff and t.is_conviction
    ]
    if not in_window:
        return InsiderSummary(
            ins_id=ins_id,
            net_value_sek=0, gross_buy_value_sek=0, gross_sell_value_sek=0,
            distinct_buyers=0, distinct_sellers=0,
            buy_count=0, sell_count=0,
            largest_single_buy_sek=0, largest_single_buy_owner="",
            transactions=[],
        )

    buys = [t for t in in_window if t.is_buy]
    sells = [t for t in in_window if t.is_sell]
    # `amount_sek` is signed: positive for buys, negative for sells.
    # Express gross values as positive magnitudes for human-readability.
    gross_buy = sum(t.amount_sek for t in buys)         # already positive
    gross_sell = sum(-t.amount_sek for t in sells)      # flip sign to positive
    largest_buy = max(buys, key=lambda t: t.amount_sek, default=None)

    return InsiderSummary(
        ins_id=ins_id,
        net_value_sek=gross_buy - gross_sell,
        gross_buy_value_sek=gross_buy,
        gross_sell_value_sek=gross_sell,
        distinct_buyers=len({t.owner_name for t in buys}),
        distinct_sellers=len({t.owner_name for t in sells}),
        buy_count=len(buys),
        sell_count=len(sells),
        largest_single_buy_sek=largest_buy.amount_sek if largest_buy else 0,
        largest_single_buy_owner=largest_buy.owner_name if largest_buy else "",
        transactions=in_window,
    )


def fetch_summaries_for_universe(
    client: BorsdataClient,
    yahoo_tickers: list[str],
    window_days: int = 90,
) -> dict[str, InsiderSummary]:
    """Bulk fetch + summarize per-ticker insider data for the universe.

    Returns dict keyed by Yahoo ticker. Tickers without insider data are absent.
    """
    yahoo_to_ins = client.yahoo_to_ins_id
    targeted = [(t, yahoo_to_ins[t]) for t in yahoo_tickers if t in yahoo_to_ins]
    missing = [t for t in yahoo_tickers if t not in yahoo_to_ins]
    if missing:
        log.warning("Yahoo tickers not found in Börsdata: %s", missing[:5] + ["..."] if len(missing) > 5 else missing)

    ins_ids = [ins for _, ins in targeted]
    log.info("Fetching Börsdata insider data for %d instruments ...", len(ins_ids))
    raw_by_ins = client.get_insider_transactions(ins_ids)

    out: dict[str, InsiderSummary] = {}
    for yahoo, ins_id in targeted:
        txs = raw_by_ins.get(ins_id, [])
        summary = summarize(txs, window_days=window_days)
        if summary and (summary.buy_count > 0 or summary.sell_count > 0):
            out[yahoo] = summary
    return out


def format_summary_for_analyst(summary: InsiderSummary | None, window_days: int = 90) -> str:
    """Compact insider block for the Analyst's user message.

    Lists the top transactions so the Analyst can see who, when, how much —
    but with equity-program noise stripped out.
    """
    if not summary or (summary.buy_count == 0 and summary.sell_count == 0):
        return f"  (no conviction-grade insider transactions in the last {window_days} days)"

    net_label = "NET BUY" if summary.net_value_sek > 0 else (
        "NET SELL" if summary.net_value_sek < 0 else "FLAT"
    )
    head = (
        f"  {window_days}-day summary (equityProgram excluded): "
        f"{net_label} {abs(summary.net_value_sek):,.0f} SEK   "
        f"(gross buy {summary.gross_buy_value_sek:,.0f} / "
        f"gross sell {summary.gross_sell_value_sek:,.0f})   "
        f"{summary.distinct_buyers} distinct buyer(s) / "
        f"{summary.distinct_sellers} distinct seller(s)"
    )
    tx_lines = []
    txs_sorted = sorted(summary.transactions, key=lambda t: (t.transaction_date or date.min), reverse=True)
    for t in txs_sorted[:12]:
        kind = "BUY " if t.is_buy else "SELL"
        date_str = t.transaction_date.isoformat() if t.transaction_date else "?"
        owner = (t.owner_name + (f" ({t.owner_position})" if t.owner_position else ""))[:50]
        tx_lines.append(
            f"    {date_str}  {kind}  {owner:50s}  {abs(t.amount_sek):>12,.0f} SEK"
        )
    if len(txs_sorted) > 12:
        tx_lines.append(f"    ... and {len(txs_sorted) - 12} more")
    return head + "\n" + "\n".join(tx_lines)
