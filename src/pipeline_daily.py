"""Thin entrypoint for the DAILY cycle (Mon-Fri).

The real orchestration lives in `src.pipeline`. This shim preserves the CLI the
GitHub Actions workflow already uses and maps it to the unified pipeline in
daily (non-deep) mode. In v2 the daily cycle trades on conviction every weekday.

    uv run python -m src.pipeline_daily              # live daily cycle
    uv run python -m src.pipeline_daily --dry-run    # full decision path, no state change
    uv run python -m src.pipeline_daily --no-email   # build report only
"""

from __future__ import annotations

import argparse
import sys

from src.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily decision cycle (v2)")
    parser.add_argument("--dry-run", action="store_true", help="Run the full path but do NOT mutate state.")
    parser.add_argument("--no-email", action="store_true", help="Never email (just build the report + dashboard).")
    # Accepted for backward-compat with the old workflow; v2 always emits a pulse.
    parser.add_argument("--silent", action="store_true", help="(Deprecated, ignored.)")
    args = parser.parse_args()
    return run(deep=False, dry_run=args.dry_run, send_email_flag=not args.no_email)


if __name__ == "__main__":
    sys.exit(main())
