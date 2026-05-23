"""Manual smoke test: send a test email via Gmail SMTP.

Usage:
    uv run python scripts/smoke_test_email.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
from datetime import datetime

from src.config import STOCKHOLM_TZ
from src.reporting import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    now = datetime.now(tz=STOCKHOLM_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    body = f"""# Investing Agent — Email Smoke Test

If you can read this, Gmail SMTP delivery is working.

Sent at: {now}

This message was sent by scripts/smoke_test_email.py as part of Phase 1
plumbing verification. No portfolio decisions have been made.
"""
    send_email(
        subject="[Investing Agent] Email smoke test",
        body_markdown=body,
    )
    print("Email sent. Check your inbox.")


if __name__ == "__main__":
    main()
