"""Email delivery via Gmail SMTP.

Gmail requires an App Password (not your normal password). Generate one
at myaccount.google.com/apppasswords and put it in .env as GMAIL_APP_PASSWORD.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from src.config import settings

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465  # SSL


def send_email(subject: str, body_markdown: str, recipient: str | None = None) -> None:
    """Send a plain-text + markdown email. Raises on failure."""
    settings.require("gmail_address", "gmail_app_password")
    to_addr = recipient or settings.report_recipient or settings.gmail_address
    if not to_addr:
        raise RuntimeError("No recipient specified and REPORT_RECIPIENT is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.gmail_address
    msg["To"] = to_addr
    msg.set_content(body_markdown)
    # A trivial HTML alternative so most clients render line breaks; we keep it
    # minimal because the report is designed to be readable as plain text.
    html_body = f"<pre style='font-family: ui-monospace, Menlo, monospace; white-space: pre-wrap;'>{_escape_html(body_markdown)}</pre>"
    msg.add_alternative(html_body, subtype="html")

    log.info("Sending email to %s (subject: %s)", to_addr, subject)
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as smtp:
        smtp.login(settings.gmail_address, settings.gmail_app_password)
        smtp.send_message(msg)
    log.info("Email sent")


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --- Weekly report builder -------------------------------------------------

from datetime import date  # noqa: E402

from src.portfolio import Portfolio  # noqa: E402


def build_weekly_report(
    *,
    today: date,
    portfolio_before: Portfolio,
    portfolio_after: Portfolio,
    prices: dict[str, float],
    screener_text: str,
    analyst_full_text: list[str],
    pm_full_text: str,
    executed_trades: list[dict],
    risk_violations: list[dict],
    journal_text: str,
    dry_run: bool,
    token_usage: dict,
) -> str:
    """Assemble the weekly markdown report — what gets emailed to the user."""
    value_before = portfolio_before.value(prices)
    value_after = portfolio_after.value(prices)
    weekly_pnl = value_after - value_before
    weekly_pnl_pct = (weekly_pnl / value_before * 100.0) if value_before else 0.0

    inception = portfolio_after.inception_date.date().isoformat()
    inception_pnl = value_after - portfolio_after.initial_capital_sek
    inception_pnl_pct = (
        inception_pnl / portfolio_after.initial_capital_sek * 100.0
        if portfolio_after.initial_capital_sek
        else 0.0
    )

    dry_banner = (
        "> **DRY RUN** — no trades were executed and the journal was not saved. "
        "This is a preview of what the agent would do.\n\n"
        if dry_run
        else ""
    )

    if executed_trades:
        trades_block = "\n".join(
            f"- **{t['action'].upper()}** {t['shares']} × {t['ticker']} "
            f"@ {t['limit_price_sek']:.2f} SEK ({t['sleeve']}) — {t['rationale']}"
            for t in executed_trades
        )
    else:
        trades_block = "_No trades this week._"

    if risk_violations:
        violations_block = "\n".join(
            f"- ❌ PROPOSED **{v['action']}** {v['shares']} × {v['ticker']} — "
            f"blocked by `{v['rule']}`: {v['detail']}"
            for v in risk_violations
        )
    else:
        violations_block = "_(none — all proposals passed risk checks)_"

    if portfolio_after.holdings:
        holdings_lines = []
        for h in portfolio_after.holdings.values():
            px = prices.get(h.ticker, h.avg_cost)
            value = h.shares * px
            pct = value / value_after * 100.0 if value_after else 0
            pnl_pct = (px / h.avg_cost - 1) * 100.0 if h.avg_cost else 0
            holdings_lines.append(
                f"- **{h.ticker}** ({h.sector or '?'}, {h.sleeve.value})  "
                f"{h.shares:.0f} sh @ {h.avg_cost:.2f} → {px:.2f}  "
                f"= {value:,.0f} SEK ({pct:.1f}% of portfolio, {pnl_pct:+.1f}% P&L)"
            )
        holdings_block = "\n".join(holdings_lines)
    else:
        holdings_block = "_No holdings yet — portfolio is 100% cash._"

    analyst_section = "\n\n---\n\n".join(analyst_full_text) if analyst_full_text else "_(no analyst notes)_"

    # Cost estimate (very rough): see Anthropic pricing
    cost_est = _estimate_cost(token_usage)

    return f"""# Investing Agent — Weekly Report
**Week ending {today.isoformat()}**

{dry_banner}## Headline

Portfolio: **{value_after:,.0f} SEK** ({weekly_pnl_pct:+.2f}% this week, {inception_pnl_pct:+.2f}% since inception {inception})
Cash: **{portfolio_after.cash_sek:,.0f} SEK** ({portfolio_after.cash_sek / value_after * 100:.1f}%)

## This week's decisions

{trades_block}

### Blocked proposals
{violations_block}

## Holdings
{holdings_block}

## Portfolio Manager reasoning
{pm_full_text}

## Screener output
{screener_text}

## Analyst notes
{analyst_section}

## Updated journal (theses.md)
```markdown
{journal_text}
```

---
*Token usage: in={token_usage.get('input', 0):,}, out={token_usage.get('output', 0):,}, cache_read={token_usage.get('cache_read', 0):,}, cache_create={token_usage.get('cache_create', 0):,}. Estimated cost: ${cost_est:.2f}.*
"""


def _estimate_cost(usage: dict) -> float:
    """Very rough cost estimate. Uses blended Sonnet+Opus rates."""
    # Approx per-1M-token blended rates (USD): input ~$8, output ~$30, cache_read ~$1
    in_ = usage.get("input", 0)
    out = usage.get("output", 0)
    cr = usage.get("cache_read", 0)
    cc = usage.get("cache_create", 0)
    return (in_ * 8 + out * 30 + cr * 1 + cc * 10) / 1_000_000

