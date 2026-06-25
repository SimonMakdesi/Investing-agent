"""Email delivery via Gmail SMTP.

Gmail requires an App Password (not your normal password). Generate one
at myaccount.google.com/apppasswords and put it in .env as GMAIL_APP_PASSWORD.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import markdown as md_lib

from src.config import settings

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465  # SSL

# Inline CSS — Gmail strips <style> in some cases but keeps it more reliably
# when scoped tight and modest. Keep this small and conservative.
EMAIL_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  color: #222;
  max-width: 760px;
  margin: 0 auto;
  padding: 24px 20px;
  background: #ffffff;
}
h1 { font-size: 26px; margin: 0 0 4px; border-bottom: 2px solid #1f2937; padding-bottom: 6px; }
h2 { font-size: 20px; margin: 28px 0 10px; color: #1f2937; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; }
h3 { font-size: 17px; margin: 22px 0 8px; color: #374151; }
h4 { font-size: 15px; margin: 16px 0 6px; color: #4b5563; }
p { margin: 10px 0; }
ul, ol { margin: 10px 0; padding-left: 24px; }
li { margin: 4px 0; }
strong { color: #111827; }
em { color: #6b7280; }
blockquote {
  border-left: 4px solid #fbbf24;
  background: #fffbeb;
  margin: 14px 0;
  padding: 10px 14px;
  color: #78350f;
  border-radius: 4px;
}
code {
  background: #f3f4f6;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px;
  color: #be185d;
}
pre {
  background: #f9fafb;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 12px 14px;
  overflow-x: auto;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.5;
  color: #111827;
}
pre code { background: transparent; padding: 0; color: inherit; }
table { border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 14px; }
th, td { border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 28px 0; }
a { color: #1d4ed8; }
"""


def _markdown_to_html(md: str) -> str:
    html_body = md_lib.markdown(md, extensions=["tables", "fenced_code"])
    return (
        "<!DOCTYPE html><html><head>"
        f"<meta charset='utf-8'><style>{EMAIL_CSS}</style>"
        "</head><body>"
        f"{html_body}"
        "</body></html>"
    )


def send_email(subject: str, body_markdown: str, recipient: str | None = None) -> None:
    """Send the email with both plain-text (markdown source) and styled HTML alternatives.

    Email clients that support HTML (Gmail, Apple Mail, Outlook) render the
    styled version. Plain-text clients see the original markdown source.
    """
    settings.require("gmail_address", "gmail_app_password")
    to_addr = recipient or settings.report_recipient or settings.gmail_address
    if not to_addr:
        raise RuntimeError("No recipient specified and REPORT_RECIPIENT is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.gmail_address
    msg["To"] = to_addr
    msg.set_content(body_markdown)
    msg.add_alternative(_markdown_to_html(body_markdown), subtype="html")

    log.info("Sending email to %s (subject: %s)", to_addr, subject)
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as smtp:
        smtp.login(settings.gmail_address, settings.gmail_app_password)
        smtp.send_message(msg)
    log.info("Email sent")


# --- Report builder (v2: terse, no essays) ---------------------------------

from datetime import date  # noqa: E402

from src.pace import Pace  # noqa: E402
from src.portfolio import Portfolio  # noqa: E402


def build_report(
    *,
    today: date,
    deep: bool,
    portfolio_before: Portfolio,
    portfolio_after: Portfolio,
    prices: dict[str, float],
    pace: Pace,
    executed_trades: list[dict],
    risk_violations: list[dict],
    trader_summary: str = "",  # accepted but intentionally NOT dumped (no essays)
    journal_text: str = "",     # included only on deep cycles
    dry_run: bool = False,
    token_usage: dict | None = None,
    contributed_this_cycle: float = 0.0,
    total_contributed: float = 0.0,
    invested_gain_sek: float = 0.0,
) -> str:
    """Assemble the terse markdown report. v2: what was done, sizes, and the
    deposit-neutral performance. Deposits are kept strictly separate from gains
    (CLAUDE.md §2/§7). No rationale essays."""
    token_usage = token_usage or {}
    value_before = portfolio_before.value(prices)
    value_after = portfolio_after.value(prices)
    # Strip this cycle's deposit out of the move so a top-up isn't shown as a gain.
    cycle_pnl = value_after - value_before - contributed_this_cycle
    cycle_pnl_pct = (cycle_pnl / value_before * 100.0) if value_before else 0.0
    cash_pct = portfolio_after.cash_sek / value_after * 100.0 if value_after else 0.0

    title = "Weekly Deep Review" if deep else "Daily Pulse"
    dry_banner = (
        "> **DRY RUN** — no trades executed, state not saved. Preview only.\n\n" if dry_run else ""
    )

    if executed_trades:
        trades_block = "\n".join(
            f"- **{t['action'].upper()}** {t['shares']} × {t['ticker']} "
            f"@ {t['limit_price_sek']:.2f} SEK — {t.get('rationale', '')}"
            for t in executed_trades
        )
    else:
        trades_block = "_No trades this cycle._"

    if risk_violations:
        violations_block = "\n".join(
            f"- ❌ {v.get('action')} {v.get('shares')} × {v.get('ticker')} — "
            f"blocked by `{v['rule']}`: {v['detail']}"
            for v in risk_violations
        )
    else:
        violations_block = ""

    if portfolio_after.holdings:
        rows = ["| Ticker | Sector | Shares | Cost | Px | Value SEK | % | P&L |",
                "|---|---|---:|---:|---:|---:|---:|---:|"]
        for h in portfolio_after.holdings.values():
            px = prices.get(h.ticker, h.avg_cost)
            value = h.shares * px
            pct = value / value_after * 100.0 if value_after else 0
            pnl_h = (px / h.avg_cost - 1) * 100.0 if h.avg_cost else 0
            rows.append(
                f"| {h.ticker} | {h.sector or '?'} | {h.shares:.0f} | {h.avg_cost:.2f} | "
                f"{px:.2f} | {value:,.0f} | {pct:.1f}% | {pnl_h:+.1f}% |"
            )
        holdings_block = "\n".join(rows)
    else:
        holdings_block = "_No holdings — 100% cash._"

    cost_est = _estimate_cost(token_usage)

    parts = [
        f"# Investing Agent — {title}",
        f"**{today.isoformat()}**",
        "",
        dry_banner.rstrip(),
        "## Where we stand",
        "",
        f"- **Portfolio value**: {value_after:,.0f} SEK",
        f"- **Deposits in (your money)**: {total_contributed:,.0f} SEK"
        + (f"  ·  +{contributed_this_cycle:,.0f} added this cycle" if contributed_this_cycle else ""),
        f"- **Invested gain (AI performance)**: {invested_gain_sek:+,.0f} SEK",
        f"- **Floor pace (deposit-neutral)**: {pace.one_liner()}",
        f"- **This cycle, ex-deposit**: {cycle_pnl:+,.0f} SEK ({cycle_pnl_pct:+.2f}%)",
        f"- **Cash**: {portfolio_after.cash_sek:,.0f} SEK ({cash_pct:.1f}%)",
        "",
        "## Decisions",
        "",
        trades_block,
    ]
    if violations_block:
        parts += ["", "### Blocked by risk checker", "", violations_block]
    parts += ["", "## Holdings", "", holdings_block]
    if deep and journal_text:
        parts += ["", "## Journal (theses.md)", "", "```markdown", journal_text, "```"]
    parts += [
        "",
        "---",
        f"*Tokens: in={token_usage.get('input', 0):,}, out={token_usage.get('output', 0):,}, "
        f"cache_read={token_usage.get('cache_read', 0):,}, cache_create={token_usage.get('cache_create', 0):,}. "
        f"Est. cost: ${cost_est:.2f}.*",
    ]
    return "\n".join(p for p in parts if p is not None) + "\n"


def _estimate_cost(usage: dict) -> float:
    """Rough cost estimate. Blended Opus+Haiku rates (USD per 1M tokens)."""
    in_ = usage.get("input", 0)
    out = usage.get("output", 0)
    cr = usage.get("cache_read", 0)
    cc = usage.get("cache_create", 0)
    return (in_ * 6 + out * 22 + cr * 0.6 + cc * 7.5) / 1_000_000

