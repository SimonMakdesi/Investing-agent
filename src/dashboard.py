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

    # Benchmark history (^OMX)
    bm_hist_df = yf.download(BENCHMARK_TICKER, period="2y", progress=False, auto_adjust=False)
    bm_hist: dict[date, float] = {}
    if bm_hist_df is not None and not bm_hist_df.empty:
        # yfinance returns a multi-index when downloading; flatten
        if hasattr(bm_hist_df.columns, "get_level_values"):
            try:
                bm_close = bm_hist_df["Close"]
                if hasattr(bm_close, "columns"):
                    bm_close = bm_close.iloc[:, 0]
            except KeyError:
                bm_close = bm_hist_df.iloc[:, 0]
        else:
            bm_close = bm_hist_df["Close"]
        bm_hist = {ts.date(): float(p) for ts, p in bm_close.dropna().items()}

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
        f"<tr><td class='font-mono'>{html.escape(h.ticker)}</td>"
        f"<td>{html.escape(h.sector)}</td>"
        f"<td><span class='inline-block px-2 py-0.5 rounded text-xs "
        f"{'bg-blue-100 text-blue-800' if h.sleeve == 'core' else 'bg-orange-100 text-orange-800'}'>{html.escape(h.sleeve)}</span></td>"
        f"<td class='text-right'>{h.shares:.0f}</td>"
        f"<td class='text-right'>{h.avg_cost:.2f}</td>"
        f"<td class='text-right'>{h.current_price:.2f}</td>"
        f"<td class='text-right'>{h.value_sek:,.0f}</td>"
        f"<td class='text-right'>{h.pct_of_portfolio:.1f}%</td>"
        f"<td class='text-right font-semibold {'text-green-600' if h.pnl_pct >= 0 else 'text-red-600'}'>{h.pnl_pct:+.1f}%</td>"
        f"<td>{_status_badge(h.thesis_status)}</td>"
        f"<td class='text-xs text-gray-500'>{h.opened_at}</td></tr>"
        for h in holdings
    ) or "<tr><td colspan='11' class='text-center text-gray-400 py-8'>No holdings yet — portfolio is 100% cash.</td></tr>"

    decision_rows = "\n".join(
        f"<tr class='hover:bg-gray-50'>"
        f"<td class='text-xs text-gray-500 whitespace-nowrap'>{html.escape(d.iso_date)}</td>"
        f"<td class='font-mono text-sm'>{html.escape(d.ticker)}</td>"
        f"<td>{_role_badge(d.role)}</td>"
        f"<td class='text-sm'>{html.escape(d.headline)}</td>"
        f"<td class='text-xs text-gray-600'>{html.escape(d.detail)}</td></tr>"
        for d in decisions[:200]
    ) or "<tr><td colspan='5' class='text-center text-gray-400 py-8'>No decisions yet.</td></tr>"

    generated_at = datetime.now().isoformat(timespec="minutes")
    inception = portfolio.inception_date.date().isoformat()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Investing Agent — Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f9fafb; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  table {{ width: 100%; }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase; color: #6b7280; font-weight: 600; padding: 10px 8px; border-bottom: 1px solid #e5e7eb; }}
  td {{ padding: 10px 8px; border-bottom: 1px solid #f3f4f6; font-size: 14px; }}
</style>
</head>
<body class="text-gray-800">

<div class="max-w-7xl mx-auto p-6 space-y-6">

  <header class="flex items-end justify-between flex-wrap gap-2">
    <div>
      <h1 class="text-3xl font-bold">Investing Agent</h1>
      <p class="text-sm text-gray-500">Inception {inception} · Generated {generated_at}</p>
    </div>
    <div class="text-right">
      <div class="text-3xl font-bold {'text-green-600' if pnl_pct >= 0 else 'text-red-600'}">{current_value:,.0f} SEK</div>
      <div class="text-sm text-gray-600">
        Portfolio <span class="{'text-green-600' if pnl_pct >= 0 else 'text-red-600'} font-semibold">{pnl_pct:+.2f}%</span>
        · OMXS30 <span class="{'text-green-600' if bm_pnl_pct >= 0 else 'text-red-600'} font-semibold">{bm_pnl_pct:+.2f}%</span>
        · Excess <span class="{'text-green-600' if excess_pct >= 0 else 'text-red-600'} font-semibold">{excess_pct:+.2f}%</span>
      </div>
    </div>
  </header>

  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
    <div class="card lg:col-span-2">
      <h2 class="text-lg font-semibold mb-3">Portfolio value vs OMXS30</h2>
      <canvas id="valueChart" height="120"></canvas>
    </div>
    <div class="card">
      <h2 class="text-lg font-semibold mb-3">Allocation</h2>
      <div class="grid grid-cols-2 gap-4">
        <div>
          <div class="text-xs text-gray-500 uppercase mb-2">Sleeve</div>
          <canvas id="sleeveChart" height="160"></canvas>
        </div>
        <div>
          <div class="text-xs text-gray-500 uppercase mb-2">Sector</div>
          <canvas id="sectorChart" height="160"></canvas>
        </div>
      </div>
      <div class="mt-4 text-sm space-y-1">
        <div class="flex justify-between"><span class="text-gray-500">Cash</span><span class="font-medium">{portfolio.cash_sek:,.0f} SEK ({cash_pct:.1f}%)</span></div>
        <div class="flex justify-between"><span class="text-gray-500">Equity</span><span class="font-medium">{current_value - portfolio.cash_sek:,.0f} SEK ({100 - cash_pct:.1f}%)</span></div>
        <div class="flex justify-between"><span class="text-gray-500">Holdings</span><span class="font-medium">{len(holdings)}</span></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2 class="text-lg font-semibold mb-3">Holdings</h2>
    <div class="overflow-x-auto">
      <table>
        <thead><tr>
          <th>Ticker</th><th>Sector</th><th>Sleeve</th>
          <th class="text-right">Shares</th><th class="text-right">Avg cost</th><th class="text-right">Price</th>
          <th class="text-right">Value</th><th class="text-right">% port</th><th class="text-right">P&amp;L</th>
          <th>Thesis</th><th>Opened</th>
        </tr></thead>
        <tbody>{holdings_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2 class="text-lg font-semibold mb-3">Decision history</h2>
    <p class="text-sm text-gray-500 mb-2">Most recent first. Includes every Analyst verdict, PM proposal, and executed trade.</p>
    <div class="overflow-x-auto">
      <table>
        <thead><tr>
          <th>Date</th><th>Ticker</th><th>Role</th><th>Headline</th><th>Context</th>
        </tr></thead>
        <tbody>{decision_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2 class="text-lg font-semibold mb-3">Anthropic token usage</h2>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
      <div><div class="text-gray-500 text-xs uppercase">Total calls</div><div class="text-xl font-semibold">{token_usage["calls"]}</div></div>
      <div><div class="text-gray-500 text-xs uppercase">Input tokens</div><div class="text-xl font-semibold">{token_usage["input_tokens"]:,}</div></div>
      <div><div class="text-gray-500 text-xs uppercase">Output tokens</div><div class="text-xl font-semibold">{token_usage["output_tokens"]:,}</div></div>
      <div><div class="text-gray-500 text-xs uppercase">Cache read</div><div class="text-xl font-semibold">{token_usage["cache_read_tokens"]:,}</div></div>
      <div><div class="text-gray-500 text-xs uppercase">Est. cost (USD)</div><div class="text-xl font-semibold">${token_usage["estimated_usd"]:.2f}</div></div>
    </div>
  </div>

  <footer class="text-xs text-gray-400 text-center pt-4 pb-8">
    Personal paper portfolio · Not investment advice · Auto-generated by GitHub Actions
  </footer>

</div>

<script>
const series = {series_json};
const sleeveData = {sleeve_json};
const sectorData = {sector_json};
const palette = ["#1d4ed8","#f59e0b","#10b981","#ef4444","#8b5cf6","#06b6d4","#ec4899","#84cc16","#6b7280","#f97316","#0ea5e9","#a855f7"];

new Chart(document.getElementById('valueChart'), {{
  type: 'line',
  data: {{
    labels: series.map(p => p.date),
    datasets: [
      {{label: 'Portfolio', data: series.map(p => p.portfolio), borderColor: '#1d4ed8', backgroundColor: 'rgba(29,78,216,0.08)', fill: true, tension: 0.2, pointRadius: 2}},
      {{label: 'OMXS30 (normalized)', data: series.map(p => p.benchmark), borderColor: '#6b7280', borderDash: [6, 4], fill: false, tension: 0.2, pointRadius: 0}},
    ],
  }},
  options: {{
    responsive: true,
    interaction: {{mode: 'index', intersect: false}},
    scales: {{y: {{ticks: {{callback: v => v.toLocaleString() + ' kr'}}}}}},
    plugins: {{legend: {{position: 'bottom'}}}},
  }},
}});

function donut(canvasId, data) {{
  const labels = Object.keys(data);
  const values = labels.map(k => data[k]);
  new Chart(document.getElementById(canvasId), {{
    type: 'doughnut',
    data: {{labels, datasets: [{{data: values, backgroundColor: palette.slice(0, labels.length)}}]}},
    options: {{plugins: {{legend: {{position: 'bottom', labels: {{boxWidth: 10, font: {{size: 11}}}}}}}}}},
  }});
}}
donut('sleeveChart', sleeveData);
donut('sectorChart', sectorData);
</script>

</body>
</html>
"""


def _status_badge(status: str) -> str:
    color_map = {
        "intact": "bg-green-100 text-green-800",
        "weakening": "bg-yellow-100 text-yellow-800",
        "broken": "bg-red-100 text-red-800",
        "unknown": "bg-gray-100 text-gray-600",
    }
    cls = color_map.get(status.split()[0] if status else "unknown", "bg-gray-100 text-gray-600")
    return (
        f"<span class='inline-block px-2 py-0.5 rounded text-xs {cls}'>"
        f"{html.escape(status or 'unknown')}</span>"
    )


def _role_badge(role: str) -> str:
    color_map = {
        "analyst": "bg-indigo-100 text-indigo-800",
        "portfolio_manager": "bg-emerald-100 text-emerald-800",
        "executed": "bg-blue-100 text-blue-800",
    }
    cls = color_map.get(role, "bg-gray-100 text-gray-600")
    label = {"portfolio_manager": "PM"}.get(role, role)
    return (
        f"<span class='inline-block px-2 py-0.5 rounded text-xs {cls}'>"
        f"{html.escape(label)}</span>"
    )


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
