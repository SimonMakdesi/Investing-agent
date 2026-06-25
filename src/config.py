"""Centralized configuration: paths, timezone, env vars.

Import `settings` from here rather than reading os.environ directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
REPORTS_DIR = REPO_ROOT / "reports"
ARCHIVE_DIR = REPO_ROOT / "archive"
DOSSIERS_DIR = STATE_DIR / "dossiers"

PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
THESES_FILE = STATE_DIR / "theses.md"
TRANSACTIONS_LOG = STATE_DIR / "transactions.log"

STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")

# --- v2 aggressive mandate knobs (CLAUDE.md §2/§4) ---
# North-star return target and horizon. Drives the pace line shown to the
# Trader and in reports. It is a north star, NOT a hard driver — see CLAUDE.md.
TARGET_RETURN_PCT = 50.0
TARGET_HORIZON_MONTHS = 6

# Monthly external top-up: the owner adds this much fake SEK once per calendar
# month for the agent to deploy. Tracked as a contribution (NOT a gain) so the
# % return measures investing skill, not deposits. See portfolio.contribute /
# pace.time_weighted_return.
MONTHLY_CONTRIBUTION_SEK = 25_000.0

# Liquidity floor: drop names whose average daily turnover (close*volume over
# 30d, native currency) is below this, so the Scout never picks untradeable
# micro-caps. Deliberately lenient — it trims junk, not real small-caps.
MIN_AVG_TURNOVER = 2_000_000.0  # ~2M native-currency units of daily turnover

# Load .env from repo root for local runs. In GitHub Actions, env vars
# come from repo Secrets and this is a no-op.
load_dotenv(REPO_ROOT / ".env")


class Settings(BaseModel):
    """Runtime configuration, sourced from env vars."""

    anthropic_api_key: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    borsdata_api_key: str = Field(default_factory=lambda: os.getenv("BORSDATA_API_KEY", ""))
    gmail_address: str = Field(default_factory=lambda: os.getenv("GMAIL_ADDRESS", ""))
    gmail_app_password: str = Field(default_factory=lambda: os.getenv("GMAIL_APP_PASSWORD", ""))
    report_recipient: str = Field(default_factory=lambda: os.getenv("REPORT_RECIPIENT", ""))

    def require(self, *fields: str) -> None:
        """Raise if any of the listed env vars are missing. Call at script start."""
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Set them in .env (local) or GitHub Secrets (CI)."
            )


settings = Settings()
