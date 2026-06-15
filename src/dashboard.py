"""Static HTML dashboard generator.

Reads `state/portfolio.json`, `state/transactions.log`, and the archived
Claude calls to produce a single self-contained HTML page at `docs/index.html`.

Designed for GitHub Pages: the page is static, loads its data from inlined
JSON in the same file, and uses Chart.js + Tailwind via CDN — no build step.

Call `build_and_write_dashboard()` at the end of the weekly pipeline.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from src.config import ARCHIVE_DIR, REPO_ROOT, STATE_DIR, TRANSACTIONS_LOG
from src.data.benchmark import blended_index_hist, regional_weights
from src.data.prices import get_history
from src.portfolio import Portfolio

log = logging.getLogger(__name__)

DOCS_DIR = REPO_ROOT / "docs"
DASHBOARD_HTML = DOCS_DIR / "index.html"
BENCHMARK_TICKER = "^OMX"  # OMX Stockholm 30


@dataclass
class TimeSeriesPoint:
    iso_date: str
    portfolio_sek: float
    benchmark_normalized_sek: float


@dataclass
class HoldingView:
    ticker: str
    sector: str
    sleeve: str
    shares: float
    avg_cost: float
    current_price: float
    value_sek: float
    pct_of_portfolio: float
    pnl_pct: float
    opened_at: str
    thesis_status: str  # intact | weakening | broken | unknown


@dataclass
class DecisionView:
    iso_date: str
    role: str           # analyst | portfolio_manager
    ticker: str
    headline: str       # e.g. "BUY 29 sh" or "INTERESTING — fits Core sleeve (conv 3)"
    detail: str         # one-line context


# --- Data loading helpers -------------------------------------------------

def _load_transactions() -> list[dict]:
    if not TRANSACTIONS_LOG.exists():
        return []
    out = []
    for line in TRANSACTIONS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed transaction line: %s", line[:80])
    return out


def _load_archived_claude_calls() -> list[dict]:
    """Iterate every JSON file under archive/claude_calls/."""
    arc_dir = ARCHIVE_DIR / "claude_calls"
    if not arc_dir.exists():
        return []
    out = []
    for path in sorted(arc_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Skipping malformed Claude archive %s: %s", path.name, e)
    return out


def _count_archived_calls() -> int:
    """Total count of archived Claude calls (filenames only; no JSON parsing)."""
    arc_dir = ARCHIVE_DIR / "claude_calls"
    if not arc_dir.exists():
        return 0
    return sum(1 for _ in arc_dir.glob("*.json"))


# --- Portfolio value over time --------------------------------------------

def _build_value_series(portfolio: Portfolio, transactions: list[dict]) -> list[TimeSeriesPoint]:
    """Replay transactions weekly and compute portfolio value at each Sunday close.

    For each weekly snapshot we use the held tickers' prices on that Sunday
    (or last available trading day).
    """
    start = portfolio.inception_date.date()
    today = date.today()
    weeks: list[date] = []
    d = start
    while d <= today:
        weeks.append(d)
        d += timedelta(days=7)
    if weeks[-1] != today:
        weeks.append(today)

    # Group transactions by date (Sunday bucket)
    txs_by_date: dict[date, list[dict]] = {}
    for tx in transactions:
        try:
            ts = datetime.fromisoformat(tx["ts"]).date()
        except (KeyError, ValueError):
            continue
        txs_by_date.setdefault(ts, []).append(tx)

    # Fetch price history once per ticker that ever appeared
    ticker_set = {tx.get("ticker") for tx in transactions if tx.get("ticker")}
    price_hist: dict[str, dict[date, float]] = {}
    span_days = max(30, (today - start).days + 14)
    for t in ticker_set:
        h = get_history(t, days=span_days)
        if h.empty or "Close" not in h.columns:
            continue
        price_hist[t] = {ts.date(): float(price) for ts, price in h["Close"].dropna().items()}

    # Benchmark history — blended OMXS30 + S&P 500, weighted by current regional
    # equity split (S&P measured in SEK). Returned as a normalised index so the
    # `bm_now / bm_start` math below is unchanged.
    se_w, us_w = regional_weights(portfolio, {})
    bm_hist: dict[date, float] = blended_index_hist(se_w, us_w, period="2y")

    def closest_price(ticker: str, target: date) -> float | None:
        hist = price_hist.get(ticker)
        if not hist:
            return None
        for off in range(0, 8):
            d = target - timedelta(days=off)
            if d in hist:
                return hist[d]
        return None

    def closest_bm(target: date) -> float | None:
        for off in range(0, 8):
            d = target - timedelta(days=off)
            if d in bm_hist:
                return bm_hist[d]
        return None

    # Replay
    cash = portfolio.initial_capital_sek
    holdings: dict[str, float] = {}  # ticker -> shares

    # Build dict from date->sorted txs so we apply in order
    bm_start = closest_bm(start) or 1.0
    series: list[TimeSeriesPoint] = []
    for snap_date in weeks:
        # Apply transactions up to and including snap_date
        for d in sorted(txs_by_date):
            if d > snap_date:
                continue
            for tx in txs_by_date[d]:
                action = tx.get("action")
                ticker = tx.get("ticker")
                shares = float(tx.get("shares") or 0)
                if action == "buy":
                    cost = float(tx.get("cost_sek") or 0)
                    cash -= cost
                    holdings[ticker] = holdings.get(ticker, 0) + shares
                elif action == "sell":
                    proceeds = float(tx.get("proceeds_sek") or 0)
                    cash += proceeds
                    if ticker in holdings:
                        holdings[ticker] = max(0, holdings[ticker] - shares)
                        if holdings[ticker] < 1e-9:
                            del holdings[ticker]
            # Mark these as applied
            txs_by_date[d] = []

        # Compute total value at snap_date
        equity_value = 0.0
        for t, sh in holdings.items():
            px = closest_price(t, snap_date)
            if px is not None:
                equity_value += sh * px
        portfolio_value = cash + equity_value

        bm_now = closest_bm(snap_date)
        bm_value = (bm_now / bm_start) * portfolio.initial_capital_sek if bm_now and bm_start else portfolio.initial_capital_sek

        series.append(TimeSeriesPoint(
            iso_date=snap_date.isoformat(),
            portfolio_sek=round(portfolio_value, 0),
            benchmark_normalized_sek=round(bm_value, 0),
        ))
    return series


# --- Holdings view --------------------------------------------------------

def _build_holdings_view(portfolio: Portfolio, theses_text: str) -> list[HoldingView]:
    out: list[HoldingView] = []
    if not portfolio.holdings:
        return out

    # Fetch current prices
    tickers = list(portfolio.holdings.keys())
    prices: dict[str, float] = {}
    for t in tickers:
        h = get_history(t, days=5)
        if not h.empty and "Close" in h.columns:
            prices[t] = float(h["Close"].dropna().iloc[-1])

    total = portfolio.cash_sek + sum(
        h.shares * prices.get(h.ticker, h.avg_cost) for h in portfolio.holdings.values()
    )
    for h in portfolio.holdings.values():
        px = prices.get(h.ticker, h.avg_cost)
        value = h.shares * px
        pct = value / total * 100 if total else 0
        pnl_pct = (px / h.avg_cost - 1) * 100 if h.avg_cost else 0
        status = _extract_thesis_status(theses_text, h.ticker)
        out.append(HoldingView(
            ticker=h.ticker, sector=h.sector or "?", sleeve=h.sleeve.value,
            shares=h.shares, avg_cost=h.avg_cost, current_price=px,
            value_sek=value, pct_of_portfolio=pct, pnl_pct=pnl_pct,
            opened_at=h.opened_at.date().isoformat(), thesis_status=status,
        ))
    return out


def _extract_thesis_status(theses_text: str, ticker: str) -> str:
    """Pull `intact|weakening|broken` from the journal for a given ticker."""
    if not theses_text or not ticker:
        return "unknown"
    import re
    pattern = re.compile(
        rf"\*\*{re.escape(ticker)}\*\*.*?\*\*Status:\*\*\s*`?(\w+(?:\s+\w+)?)`?",
        re.DOTALL,
    )
    m = pattern.search(theses_text)
    if not m:
        return "unknown"
    return m.group(1).lower().strip()


# --- Decision history -----------------------------------------------------

def _build_decisions_view(claude_calls: list[dict], transactions: list[dict]) -> list[DecisionView]:
    out: list[DecisionView] = []
    for call in claude_calls:
        role = call.get("role", "?")
        ts = call.get("timestamp", "")
        try:
            iso_date = datetime.fromisoformat(ts).date().isoformat()
        except ValueError:
            iso_date = ts[:10]
        text = call.get("response_text", "") or ""

        if role == "analyst":
            ticker, verdict, conv = _parse_analyst_json(text)
            if ticker:
                out.append(DecisionView(
                    iso_date=iso_date, role=role, ticker=ticker,
                    headline=f"{verdict or 'verdict?'} (conv {conv or '?'})",
                    detail=_first_sentence_of_thesis(text),
                ))
        elif role == "portfolio_manager":
            for trade in _parse_pm_trades(text):
                out.append(DecisionView(
                    iso_date=iso_date, role=role, ticker=trade.get("ticker", "?"),
                    headline=(
                        f"{trade.get('action', '?').upper()} {trade.get('shares', '?')} sh "
                        f"@ {trade.get('limit_price_sek', '?')} SEK"
                    ),
                    detail=trade.get("rationale", "")[:200],
                ))

    # Also include executed trades from the transactions log (source of truth)
    for tx in transactions:
        if tx.get("action") in ("buy", "sell"):
            ts = tx.get("ts", "")
            try:
                iso_date = datetime.fromisoformat(ts).date().isoformat()
            except ValueError:
                iso_date = ts[:10]
            out.append(DecisionView(
                iso_date=iso_date, role="executed",
                ticker=tx.get("ticker", "?"),
                headline=f"{tx.get('action', '?').upper()} {tx.get('shares', 0):.0f} sh @ {tx.get('price', 0):.2f}",
                detail=(tx.get("rationale") or "")[:200],
            ))

    # Most recent first
    out.sort(key=lambda d: d.iso_date, reverse=True)
    return out


def _parse_analyst_json(text: str) -> tuple[str | None, str | None, str | None]:
    try:
        from src.json_parse import extract_json
        d = extract_json(text)
        return d.get("ticker"), d.get("verdict"), str(d.get("conviction", ""))
    except Exception:
        return None, None, None


def _parse_pm_trades(text: str) -> list[dict]:
    try:
        from src.json_parse import extract_json
        d = extract_json(text)
        return d.get("trades", []) or []
    except Exception:
        return []


def _first_sentence_of_thesis(text: str) -> str:
    """Grab a meaningful one-liner from the analyst's prose."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if line.startswith("**Thesis"):
            # Look at the next non-empty line as the actual thesis
            idx = lines.index(line)
            for nxt in lines[idx + 1 : idx + 4]:
                if nxt and not nxt.startswith("**"):
                    return nxt[:240]
        if "thesis" in line.lower() and len(line) > 50:
            return line[:240]
    return (lines[0] if lines else "")[:240]


# --- Token usage tracking -------------------------------------------------

def _compute_token_usage(claude_calls: list[dict]) -> dict:
    total_in = total_out = total_cr = total_cc = 0
    for c in claude_calls:
        u = c.get("usage", {})
        total_in += u.get("input_tokens", 0) or 0
        total_out += u.get("output_tokens", 0) or 0
        total_cr += u.get("cache_read_tokens", 0) or 0
        total_cc += u.get("cache_creation_tokens", 0) or 0
    # Rough blended cost estimate (Sonnet + Opus mix). Real cost is on console.
    est_usd = (total_in * 8 + total_out * 30 + total_cr * 1 + total_cc * 10) / 1_000_000
    return {
        "calls": len(claude_calls),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cr,
        "cache_creation_tokens": total_cc,
        "estimated_usd": round(est_usd, 2),
    }


# --- HTML rendering -------------------------------------------------------

def _render_html(
    portfolio: Portfolio,
    series: list[TimeSeriesPoint],
    holdings: list[HoldingView],
    decisions: list[DecisionView],
    token_usage: dict,
) -> str:
    # Headline numbers
    latest = series[-1] if series else None
    current_value = latest.portfolio_sek if latest else portfolio.initial_capital_sek
    benchmark_value = latest.benchmark_normalized_sek if latest else portfolio.initial_capital_sek
    pnl_pct = (current_value / portfolio.initial_capital_sek - 1) * 100
    bm_pnl_pct = (benchmark_value / portfolio.initial_capital_sek - 1) * 100
    excess_pct = pnl_pct - bm_pnl_pct
    cash_pct = (portfolio.cash_sek / current_value * 100) if current_value else 0

    # Sleeve & sector aggregations
    sleeve_alloc = {"Core": 0.0, "Aggressive": 0.0, "Cash": portfolio.cash_sek}
    sector_alloc: dict[str, float] = {}
    for h in holdings:
        sleeve_key = "Core" if h.sleeve == "core" else "Aggressive"
        sleeve_alloc[sleeve_key] += h.value_sek
        sector_alloc[h.sector] = sector_alloc.get(h.sector, 0.0) + h.value_sek
    if portfolio.cash_sek > 0:
        sector_alloc["Cash"] = portfolio.cash_sek

    series_json = json.dumps([{
        "date": s.iso_date,
        "portfolio": s.portfolio_sek,
        "benchmark": s.benchmark_normalized_sek,
    } for s in series])
    sleeve_json = json.dumps(sleeve_alloc)
    sector_json = json.dumps(sector_alloc)

    holdings_rows = "\n".join(
        f"<tr>"
        f"<td class='num font-semibold' style='color: var(--ink); letter-spacing:0.04em;'>{html.escape(h.ticker)}</td>"
        f"<td class='script' style='color: var(--ink-fade);'>{html.escape(h.sector)}</td>"
        f"<td>{_guild_mark(h.sleeve)}</td>"
        f"<td class='num text-right'>{h.shares:.0f}</td>"
        f"<td class='num text-right'>{h.avg_cost:.2f}</td>"
        f"<td class='num text-right'>{h.current_price:.2f}</td>"
        f"<td class='num text-right font-semibold'>{h.value_sek:,.0f}</td>"
        f"<td class='num text-right'>{h.pct_of_portfolio:.1f}%</td>"
        f"<td class='num text-right font-bold {'ledger-positive' if h.pnl_pct >= 0 else 'ledger-negative'}'>"
        f"{'▲' if h.pnl_pct >= 0 else '▼'} {h.pnl_pct:+.1f}%</td>"
        f"<td>{_status_badge(h.thesis_status)}</td>"
        f"<td class='num text-right text-xs' style='color: var(--ink-fade);'>{h.opened_at}</td></tr>"
        for h in holdings
    ) or (
        "<tr><td colspan='11' class='text-center py-10 script' "
        "style='color: var(--ink-fade);'>The vault is empty save for coin — no shares are sworn.</td></tr>"
    )

    DECISION_DISPLAY_LIMIT = 30
    decisions_shown = decisions[:DECISION_DISPLAY_LIMIT]
    decision_rows = "\n".join(
        f"<tr>"
        f"<td class='num text-xs whitespace-nowrap' style='color: var(--ink-fade);'>{html.escape(d.iso_date)}</td>"
        f"<td class='num font-semibold' style='color: var(--ink);'>{html.escape(d.ticker)}</td>"
        f"<td>{_role_badge(d.role)}</td>"
        f"<td class='text-sm script' style='color: var(--ink);'>{html.escape(d.headline)}</td>"
        f"<td class='text-xs' style='color: var(--ink-fade);'>{html.escape(d.detail)}</td></tr>"
        for d in decisions_shown
    ) or (
        "<tr><td colspan='5' class='text-center py-10 script' "
        "style='color: var(--ink-fade);'>No counsel has yet been set down in this tome.</td></tr>"
    )
    decisions_footnote = (
        f"Showing the most recent <span class='num font-semibold'>{len(decisions_shown)}</span> "
        f"of <span class='num font-semibold'>{len(decisions)}</span> entries. "
        "The complete tome rests in <code>archive/claude_calls/</code> and <code>reports/</code>."
    ) if len(decisions) > DECISION_DISPLAY_LIMIT else (
        f"All <span class='num font-semibold'>{len(decisions)}</span> entries set down below."
    )

    generated_at = datetime.now().isoformat(timespec="minutes")
    inception = portfolio.inception_date.date().isoformat()

    pnl_class = "ledger-positive" if pnl_pct >= 0 else "ledger-negative"
    bm_class = "ledger-positive" if bm_pnl_pct >= 0 else "ledger-negative"
    excess_class = "ledger-positive" if excess_pct >= 0 else "ledger-negative"
    pnl_sigil = "▲" if pnl_pct >= 0 else "▼"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>The Vault Ledger · Investing Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600;700;800;900&family=IM+Fell+English:ital@0;1&family=IM+Fell+English+SC&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --ink:        #2a1f12;
    --ink-fade:   #5a4a32;
    --gold:       #c9a961;
    --gold-deep:  #8b6914;
    --gold-bright:#e8c87a;
    --bronze:     #a07d3c;
    --parchment:  #f0e3c4;
    --parchment-lit: #f7ebcd;
    --parchment-shade: #d9c9a1;
    --leather:    #1a0f08;
    --leather-lit:#241509;
    --blood:      #7c2120;
    --blood-deep: #4a1212;
    --moss:       #3d6b3a;
    --moss-deep:  #234020;
    --sapphire:   #1f3a5f;
    --amber:      #9c6b1a;
  }}

  /* ============ Body / wood-leather backdrop ============ */
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse at 30% 20%, rgba(70,40,15,0.35), transparent 60%),
      radial-gradient(ellipse at 80% 90%, rgba(40,20,5,0.5), transparent 70%),
      repeating-linear-gradient(
        103deg,
        #1a0f08 0px, #1a0f08 30px,
        #1f1209 30px, #1f1209 32px,
        #1a0f08 32px, #1a0f08 70px,
        #150a05 70px, #150a05 72px),
      #1a0f08;
    min-height: 100vh;
    background-attachment: fixed;
  }}
  body::before {{
    content: "";
    position: fixed; inset: 0;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.16 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
    opacity: 0.55;
    pointer-events: none;
    mix-blend-mode: overlay;
    z-index: 0;
  }}
  main {{ position: relative; z-index: 1; }}

  /* ============ Typography ============ */
  .display      {{ font-family: 'Cinzel', serif; letter-spacing: 0.04em; }}
  .heading      {{ font-family: 'Cinzel', serif; letter-spacing: 0.16em; text-transform: uppercase; }}
  .script       {{ font-family: 'IM Fell English', serif; font-style: italic; }}
  .smallcaps    {{ font-family: 'IM Fell English SC', serif; letter-spacing: 0.08em; }}
  .num          {{ font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }}

  /* ============ Parchment card ============ */
  .scroll {{
    position: relative;
    background:
      radial-gradient(ellipse at 50% 0%, var(--parchment-lit), transparent 60%),
      radial-gradient(ellipse at 100% 100%, var(--parchment-shade), transparent 70%),
      linear-gradient(168deg, #f5e7c6 0%, #ecdcb6 50%, #e2cf9f 100%);
    color: var(--ink);
    border-radius: 4px;
    box-shadow:
      0 0 0 1px rgba(139, 105, 20, 0.6),
      0 0 0 3px var(--leather-lit),
      0 0 0 4px rgba(201, 169, 97, 0.45),
      0 22px 50px rgba(0,0,0,0.55),
      inset 0 0 60px rgba(120, 85, 30, 0.18);
    padding: 32px 28px 28px;
  }}
  .scroll::before {{
    content: "";
    position: absolute; inset: 0;
    pointer-events: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='p'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch' seed='4'/><feColorMatrix values='0 0 0 0 0.4  0 0 0 0 0.3  0 0 0 0 0.15  0 0 0 0.18 0'/></filter><rect width='100%' height='100%' filter='url(%23p)'/></svg>");
    opacity: 0.6;
    mix-blend-mode: multiply;
    border-radius: 4px;
  }}
  .scroll::after {{
    /* gilt corner flourish */
    content: "";
    position: absolute; top: 6px; right: 6px; width: 38px; height: 38px;
    pointer-events: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40' fill='none' stroke='%238b6914' stroke-width='1.2'><path d='M2 14 Q14 14 14 2'/><path d='M2 8 Q8 8 8 2'/><circle cx='20' cy='20' r='1.5' fill='%238b6914'/></svg>");
    background-repeat: no-repeat;
    opacity: 0.6;
  }}
  .scroll > * {{ position: relative; z-index: 1; }}

  /* Each ornate section heading */
  .section-title {{
    font-family: 'Cinzel', serif;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--ink);
    font-size: 14px;
    margin-bottom: 14px;
    display: flex; align-items: center; gap: 14px;
  }}
  .section-title::before,
  .section-title::after {{
    content: "";
    flex: 1;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--gold-deep), transparent);
  }}
  .section-title .glyph {{
    color: var(--gold-deep);
    font-size: 18px;
    text-shadow: 0 0 4px rgba(201,169,97,0.5);
  }}

  /* Crest at the top */
  .crest {{
    width: 56px; height: 56px;
    background: radial-gradient(circle, #1a0f08 0%, #0d0703 70%);
    border: 2px solid var(--gold);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 0 4px var(--leather-lit), 0 0 24px rgba(232,200,122,0.4), inset 0 0 16px rgba(0,0,0,0.6);
    color: var(--gold-bright);
    font-family: 'Cinzel', serif;
    font-weight: 800;
    font-size: 22px;
  }}

  /* Hero P&L numbers — like loot drops */
  .loot-value {{
    font-family: 'Cinzel', serif;
    font-weight: 800;
    font-size: clamp(2.4rem, 6vw, 4rem);
    letter-spacing: 0.04em;
    line-height: 1;
    background: linear-gradient(180deg, #f5d97a 0%, #c9a961 55%, #8b6914 100%);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    text-shadow: 0 2px 0 rgba(0,0,0,0.25);
    filter: drop-shadow(0 0 8px rgba(232,200,122,0.35));
  }}
  .loot-currency {{
    font-family: 'Cinzel', serif;
    font-weight: 600;
    font-size: 0.42em;
    color: var(--gold);
    letter-spacing: 0.18em;
    margin-left: 6px;
    vertical-align: middle;
  }}
  .ledger-positive {{ color: var(--moss-deep); }}
  .ledger-negative {{ color: var(--blood); }}

  /* ============ Tables (ruled ledger lines) ============ */
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    text-align: left;
    font-family: 'IM Fell English SC', serif;
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--gold-deep);
    padding: 10px 10px 8px;
    border-bottom: 2px double var(--gold-deep);
    background: linear-gradient(180deg, rgba(201,169,97,0.18), transparent);
  }}
  tbody td {{
    padding: 11px 10px;
    font-size: 14px;
    border-bottom: 1px solid rgba(139,105,20,0.22);
    color: var(--ink);
  }}
  tbody tr {{
    transition: background-color 0.18s;
  }}
  tbody tr:nth-child(even) td {{ background: rgba(139,105,20,0.04); }}
  tbody tr:hover td {{ background: rgba(232,200,122,0.18); }}
  tbody tr:last-child td {{ border-bottom: 1px solid var(--gold-deep); }}

  /* ============ Heraldic badges ============ */
  .shield {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 9px 3px 7px;
    font-family: 'IM Fell English SC', serif;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    border-radius: 2px;
    border: 1px solid currentColor;
    background: rgba(255,255,255,0.35);
  }}
  .shield-intact     {{ color: var(--moss-deep);    background: rgba(61,107,58,0.13); }}
  .shield-weakening  {{ color: var(--amber);        background: rgba(156,107,26,0.13); }}
  .shield-broken     {{ color: var(--blood);        background: rgba(124,33,32,0.12); }}
  .shield-unknown    {{ color: var(--ink-fade);     background: rgba(90,74,50,0.10); }}
  .shield svg        {{ width: 14px; height: 14px; flex: 0 0 14px; }}

  .guild {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 9px;
    font-family: 'IM Fell English SC', serif;
    font-size: 10.5px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    border-radius: 2px;
    border: 1px solid currentColor;
  }}
  .guild-core         {{ color: var(--sapphire);    background: rgba(31,58,95,0.10); }}
  .guild-aggressive   {{ color: var(--blood);       background: rgba(124,33,32,0.10); }}

  .role-mark {{
    display: inline-block;
    padding: 2px 8px;
    font-family: 'IM Fell English SC', serif;
    font-size: 10.5px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    border-radius: 2px;
    border: 1px solid currentColor;
  }}
  .role-analyst   {{ color: var(--sapphire);  background: rgba(31,58,95,0.10); }}
  .role-pm        {{ color: var(--moss-deep); background: rgba(61,107,58,0.10); }}
  .role-executed  {{ color: var(--amber);     background: rgba(156,107,26,0.12); }}
  .role-other     {{ color: var(--ink-fade);  background: rgba(90,74,50,0.08); }}

  /* ============ Misc ============ */
  .divider-ornate {{
    height: 1px;
    background: linear-gradient(to right, transparent, var(--gold), transparent);
    position: relative;
    margin: 18px 0;
  }}
  .divider-ornate::before {{
    content: "❖";
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -55%);
    color: var(--gold-deep);
    background: var(--parchment-lit);
    padding: 0 12px;
    font-size: 13px;
  }}
  code {{
    font-family: 'JetBrains Mono', monospace;
    background: rgba(139,105,20,0.12);
    padding: 1px 5px;
    border-radius: 2px;
    color: var(--ink);
    font-size: 11.5px;
  }}
  a {{ color: var(--gold-deep); }}

  /* ============ Page-load reveal ============ */
  @keyframes inscribe {{
    0% {{ opacity: 0; transform: translateY(8px); }}
    100% {{ opacity: 1; transform: translateY(0); }}
  }}
  .scroll {{ animation: inscribe 0.7s ease-out both; }}
  .scroll:nth-child(2) {{ animation-delay: 0.08s; }}
  .scroll:nth-child(3) {{ animation-delay: 0.16s; }}
  .scroll:nth-child(4) {{ animation-delay: 0.24s; }}
  .scroll:nth-child(5) {{ animation-delay: 0.32s; }}

  /* Mobile padding tightening */
  @media (max-width: 640px) {{
    .scroll {{ padding: 22px 18px 18px; }}
    .loot-value {{ font-size: 2.2rem; }}
    .section-title {{ font-size: 12px; letter-spacing: 0.16em; }}
    thead th {{ padding: 8px 6px; font-size: 10px; }}
    tbody td {{ padding: 8px 6px; font-size: 12.5px; }}
  }}
</style>
</head>
<body>

<main class="max-w-7xl mx-auto p-4 sm:p-8 space-y-6">

  <!-- ============ HEADER ============ -->
  <header class="scroll">
    <div class="flex items-start justify-between flex-wrap gap-6">
      <div class="flex items-start gap-5">
        <div class="crest">A</div>
        <div>
          <div class="smallcaps text-xs" style="color: var(--gold-deep);">— Vault Ledger of the —</div>
          <h1 class="display text-3xl sm:text-4xl font-extrabold leading-tight" style="color: var(--ink);">Investing Agent</h1>
          <div class="script text-sm mt-1" style="color: var(--ink-fade);">
            Inaugurated on the <span class="num">{inception}</span> day of our reckoning ·
            Hand last set down <span class="num">{generated_at}</span>
          </div>
        </div>
      </div>
      <div class="text-right min-w-[240px]">
        <div class="smallcaps text-[11px]" style="color: var(--gold-deep);">Coffers Total</div>
        <div class="loot-value {pnl_class}">{current_value:,.0f}<span class="loot-currency">SEK</span></div>
        <div class="script mt-3 text-sm" style="color: var(--ink-fade);">
          <span class="num {pnl_class}">{pnl_sigil} {pnl_pct:+.2f}%</span> against thine own coin ·
          <span class="num {bm_class}">{bm_pnl_pct:+.2f}%</span> the blended index ·
          <span class="num {excess_class}">{excess_pct:+.2f}%</span> alpha
        </div>
      </div>
    </div>
  </header>

  <!-- ============ CHART + ALLOCATION ============ -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

    <div class="scroll lg:col-span-2">
      <h2 class="section-title"><span class="glyph">⚜</span> Treasury Through the Ages <span class="glyph">⚜</span></h2>
      <div style="position: relative; height: 340px;">
        <canvas id="valueChart"></canvas>
      </div>
    </div>

    <div class="scroll">
      <h2 class="section-title"><span class="glyph">✦</span> Coinage <span class="glyph">✦</span></h2>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="smallcaps text-[10px] text-center mb-2" style="color: var(--gold-deep);">— Guild —</div>
          <canvas id="sleeveChart" height="160"></canvas>
        </div>
        <div>
          <div class="smallcaps text-[10px] text-center mb-2" style="color: var(--gold-deep);">— Dominion —</div>
          <canvas id="sectorChart" height="160"></canvas>
        </div>
      </div>
      <div class="divider-ornate"></div>
      <div class="space-y-1.5 text-sm">
        <div class="flex justify-between"><span class="smallcaps text-xs" style="color: var(--gold-deep);">Coin in hand</span><span class="num font-semibold">{portfolio.cash_sek:,.0f} ({cash_pct:.1f}%)</span></div>
        <div class="flex justify-between"><span class="smallcaps text-xs" style="color: var(--gold-deep);">Bound in shares</span><span class="num font-semibold">{current_value - portfolio.cash_sek:,.0f} ({100 - cash_pct:.1f}%)</span></div>
        <div class="flex justify-between"><span class="smallcaps text-xs" style="color: var(--gold-deep);">Holdings</span><span class="num font-semibold">{len(holdings)}</span></div>
      </div>
    </div>

  </div>

  <!-- ============ HOLDINGS ============ -->
  <div class="scroll">
    <h2 class="section-title"><span class="glyph">⚔</span> Sworn Holdings <span class="glyph">⚔</span></h2>
    <div class="overflow-x-auto">
      <table>
        <thead><tr>
          <th>Sigil</th><th>Dominion</th><th>Guild</th>
          <th class="text-right">Shares</th><th class="text-right">Cost</th><th class="text-right">Now</th>
          <th class="text-right">Value</th><th class="text-right">Stake</th><th class="text-right">Fortune</th>
          <th>Oath</th><th class="text-right">Sworn</th>
        </tr></thead>
        <tbody>{holdings_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ============ DECISIONS ============ -->
  <div class="scroll">
    <h2 class="section-title"><span class="glyph">✶</span> Chronicle of Counsel <span class="glyph">✶</span></h2>
    <p class="script text-sm mb-3" style="color: var(--ink-fade);">
      Most recent first — verdicts of the Analyst, decrees of the Manager, deeds set in coin.
      {decisions_footnote}
    </p>
    <div class="overflow-x-auto">
      <table>
        <thead><tr>
          <th>Day</th><th>Sigil</th><th>Hand</th><th>Word</th><th>Counsel</th>
        </tr></thead>
        <tbody>{decision_rows}</tbody>
      </table>
    </div>
  </div>

  <!-- ============ TOKEN USAGE ============ -->
  <div class="scroll">
    <h2 class="section-title"><span class="glyph">◈</span> The Oracle's Tariff <span class="glyph">◈</span></h2>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-5">
      <div>
        <div class="smallcaps text-[10px]" style="color: var(--gold-deep);">Consultations</div>
        <div class="num text-2xl font-bold" style="color: var(--ink);">{token_usage["calls"]}</div>
      </div>
      <div>
        <div class="smallcaps text-[10px]" style="color: var(--gold-deep);">Words Whispered</div>
        <div class="num text-2xl font-bold" style="color: var(--ink);">{token_usage["input_tokens"]:,}</div>
      </div>
      <div>
        <div class="smallcaps text-[10px]" style="color: var(--gold-deep);">Words Returned</div>
        <div class="num text-2xl font-bold" style="color: var(--ink);">{token_usage["output_tokens"]:,}</div>
      </div>
      <div>
        <div class="smallcaps text-[10px]" style="color: var(--gold-deep);">Memory Drawn</div>
        <div class="num text-2xl font-bold" style="color: var(--ink);">{token_usage["cache_read_tokens"]:,}</div>
      </div>
      <div>
        <div class="smallcaps text-[10px]" style="color: var(--gold-deep);">Tribute Paid</div>
        <div class="num text-2xl font-bold" style="color: var(--gold-deep);">${token_usage["estimated_usd"]:.2f}</div>
      </div>
    </div>
  </div>

  <!-- ============ FOOTER ============ -->
  <footer class="text-center pt-2 pb-10">
    <div class="script text-sm" style="color: rgba(232,200,122,0.65);">
      ❦ A ledger of paper coin, kept for one keeper alone. Naught herein is counsel for any other soul. ❦
    </div>
    <div class="smallcaps text-[10px] mt-2" style="color: rgba(232,200,122,0.4);">
      Inscribed by the Iron Quill of GitHub Actions
    </div>
  </footer>

</main>

<script>
const series = {series_json};
const sleeveData = {sleeve_json};
const sectorData = {sector_json};

// Fantasy palette — gold, sapphire, blood, moss, bronze, parchment
const fantasyPalette = ["#c9a961","#1f3a5f","#7c2120","#3d6b3a","#a07d3c","#5a4a32","#9c6b1a","#4a3d6b","#6b3a3a","#3a6b6b"];
const ink = "#2a1f12";
const gold = "#8b6914";
const goldBright = "#c9a961";
const parchmentEdge = "rgba(139,105,20,0.22)";

Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
Chart.defaults.color = ink;

new Chart(document.getElementById('valueChart'), {{
  type: 'line',
  data: {{
    labels: series.map(p => p.date),
    datasets: [
      {{
        label: 'Thy Treasury',
        data: series.map(p => p.portfolio),
        borderColor: '#8b6914',
        backgroundColor: (ctx) => {{
          const {{ctx: c, chartArea}} = ctx.chart;
          if (!chartArea) return 'rgba(201,169,97,0.18)';
          const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          g.addColorStop(0, 'rgba(232,200,122,0.45)');
          g.addColorStop(1, 'rgba(232,200,122,0.02)');
          return g;
        }},
        fill: true,
        tension: 0.28,
        pointRadius: 3,
        pointBackgroundColor: '#8b6914',
        pointBorderColor: '#f7ebcd',
        pointBorderWidth: 1.5,
        borderWidth: 2.5,
      }},
      {{
        label: 'OMXS30 + S&P 500 blend (the realm)',
        data: series.map(p => p.benchmark),
        borderColor: 'rgba(42,31,18,0.55)',
        borderDash: [4, 5],
        fill: false,
        tension: 0.28,
        pointRadius: 0,
        borderWidth: 1.6,
      }},
    ],
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{mode: 'index', intersect: false}},
    scales: {{
      x: {{
        grid: {{ color: 'rgba(139,105,20,0.10)', drawTicks: false }},
        ticks: {{ color: ink, font: {{family: "'JetBrains Mono', monospace", size: 10}} }},
        border: {{ color: parchmentEdge }},
      }},
      y: {{
        grid: {{ color: 'rgba(139,105,20,0.10)' }},
        ticks: {{
          callback: v => v.toLocaleString() + ' kr',
          color: ink,
          font: {{family: "'JetBrains Mono', monospace", size: 10}}
        }},
        border: {{ color: parchmentEdge }},
      }},
    }},
    plugins: {{
      legend: {{
        position: 'bottom',
        labels: {{
          font: {{family: "'IM Fell English SC', serif", size: 12}},
          color: ink,
          boxWidth: 14,
          boxHeight: 8,
        }}
      }},
      tooltip: {{
        backgroundColor: '#1a0f08',
        titleColor: goldBright,
        bodyColor: '#f0e3c4',
        borderColor: gold,
        borderWidth: 1,
        titleFont: {{family: "'Cinzel', serif", size: 12}},
        bodyFont: {{family: "'JetBrains Mono', monospace", size: 12}},
        padding: 10,
      }}
    }},
  }},
}});

function donut(canvasId, data) {{
  const labels = Object.keys(data);
  const values = labels.map(k => data[k]);
  new Chart(document.getElementById(canvasId), {{
    type: 'doughnut',
    data: {{
      labels,
      datasets: [{{
        data: values,
        backgroundColor: fantasyPalette.slice(0, labels.length),
        borderColor: '#1a0f08',
        borderWidth: 1.5,
        hoverOffset: 8,
      }}]
    }},
    options: {{
      cutout: '62%',
      plugins: {{
        legend: {{
          position: 'bottom',
          labels: {{
            boxWidth: 10,
            boxHeight: 10,
            font: {{family: "'IM Fell English SC', serif", size: 10.5}},
            color: ink,
            padding: 6,
          }}
        }},
        tooltip: {{
          backgroundColor: '#1a0f08',
          titleColor: goldBright,
          bodyColor: '#f0e3c4',
          borderColor: gold,
          borderWidth: 1,
          titleFont: {{family: "'Cinzel', serif", size: 11}},
          bodyFont: {{family: "'JetBrains Mono', monospace", size: 11}},
          callbacks: {{
            label: (ctx) => `${{ctx.label}}: ${{ctx.parsed.toLocaleString()}} SEK`
          }}
        }}
      }}
    }},
  }});
}}
donut('sleeveChart', sleeveData);
donut('sectorChart', sectorData);
</script>

</body>
</html>
"""


# Heraldic shield SVGs — small inline shields tinted to currentColor so badge
# CSS controls the colour. Each conveys a different state through its emblem.
_SHIELD_LEAF = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
    "<path d='M12 2 L20 5 V12 C20 17 16 21 12 22 C8 21 4 17 4 12 V5 Z' fill='currentColor' fill-opacity='0.15'/>"
    "<path d='M12 8 C 9 11 9 15 12 17 C 15 15 15 11 12 8 Z' fill='currentColor' fill-opacity='0.55'/>"
    "<path d='M12 8 V17' stroke-width='1'/>"
    "</svg>"
)
_SHIELD_HOURGLASS = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
    "<path d='M12 2 L20 5 V12 C20 17 16 21 12 22 C8 21 4 17 4 12 V5 Z' fill='currentColor' fill-opacity='0.15'/>"
    "<path d='M9 8 H15 L11 12 L15 16 H9 L13 12 Z' fill='currentColor' fill-opacity='0.55' stroke-width='1'/>"
    "</svg>"
)
_SHIELD_CRACKED = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
    "<path d='M12 2 L20 5 V12 C20 17 16 21 12 22 C8 21 4 17 4 12 V5 Z' fill='currentColor' fill-opacity='0.15'/>"
    "<path d='M12 4 L10 9 L13 11 L9 16 L13 20' stroke-width='1.7'/>"
    "</svg>"
)
_SHIELD_QUESTION = (
    "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
    "<path d='M12 2 L20 5 V12 C20 17 16 21 12 22 C8 21 4 17 4 12 V5 Z' fill='currentColor' fill-opacity='0.1'/>"
    "<path d='M10 10 C10 8.5 11 7.5 12 7.5 C13 7.5 14 8.5 14 9.5 C14 11 12 11.5 12 13' stroke-width='1.3'/>"
    "<circle cx='12' cy='16' r='0.6' fill='currentColor'/>"
    "</svg>"
)

_STATUS_GLYPH = {
    "intact": (_SHIELD_LEAF, "shield-intact", "Intact"),
    "weakening": (_SHIELD_HOURGLASS, "shield-weakening", "Weakening"),
    "broken": (_SHIELD_CRACKED, "shield-broken", "Broken"),
    "unknown": (_SHIELD_QUESTION, "shield-unknown", "Unknown"),
}


def _status_badge(status: str) -> str:
    key = (status.split()[0] if status else "unknown").lower()
    svg, cls, label = _STATUS_GLYPH.get(key, _STATUS_GLYPH["unknown"])
    # Preserve the full status text (e.g. "broken - exit planned") on hover.
    full = html.escape(status or "unknown")
    return f"<span class='shield {cls}' title='{full}'>{svg}{label}</span>"


def _guild_mark(sleeve: str) -> str:
    """Heraldic mark for sleeve membership — Core (noble) or Aggressive (battle)."""
    sleeve = (sleeve or "").lower()
    if sleeve == "core":
        # crown for nobility
        emblem = (
            "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.3' "
            "aria-hidden='true' style='width:12px;height:12px;'>"
            "<path d='M3 17 L5 8 L9 12 L12 6 L15 12 L19 8 L21 17 Z' fill='currentColor' fill-opacity='0.25'/>"
            "<path d='M3 17 H21' stroke-width='1.5'/>"
            "</svg>"
        )
        return f"<span class='guild guild-core'>{emblem}Core</span>"
    if sleeve == "aggressive":
        # crossed swords for battle
        emblem = (
            "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.4' "
            "stroke-linecap='round' aria-hidden='true' style='width:12px;height:12px;'>"
            "<path d='M4 4 L18 18'/>"
            "<path d='M20 4 L6 18'/>"
            "<circle cx='4' cy='4' r='1.2' fill='currentColor'/>"
            "<circle cx='20' cy='4' r='1.2' fill='currentColor'/>"
            "</svg>"
        )
        return f"<span class='guild guild-aggressive'>{emblem}Aggressive</span>"
    return f"<span class='guild' style='color: var(--ink-fade);'>{html.escape(sleeve or '—')}</span>"


def _role_badge(role: str) -> str:
    role_label_map = {
        "analyst": ("Analyst", "role-analyst"),
        "portfolio_manager": ("Manager", "role-pm"),
        "executed": ("Decree", "role-executed"),
    }
    label, cls = role_label_map.get(role, (role or "—", "role-other"))
    return f"<span class='role-mark {cls}'>{html.escape(label)}</span>"


# --- Entry point ----------------------------------------------------------

def build_and_write_dashboard() -> Path:
    """Build the dashboard and write it to docs/index.html. Returns the path."""
    log.info("Building dashboard ...")
    portfolio = Portfolio.load()
    transactions = _load_transactions()
    claude_calls = _load_archived_claude_calls()
    theses_text = (STATE_DIR / "theses.md").read_text(encoding="utf-8") if (STATE_DIR / "theses.md").exists() else ""

    series = _build_value_series(portfolio, transactions)
    holdings = _build_holdings_view(portfolio, theses_text)
    decisions = _build_decisions_view(claude_calls, transactions)
    token_usage = _compute_token_usage(claude_calls)

    html_out = _render_html(portfolio, series, holdings, decisions, token_usage)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_HTML.write_text(html_out, encoding="utf-8")
    log.info("Dashboard written to %s", DASHBOARD_HTML)
    return DASHBOARD_HTML


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = build_and_write_dashboard()
    print(f"Dashboard ready: {p}")
