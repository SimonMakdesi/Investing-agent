"""Thin entrypoint for the WEEKLY DEEP review (Saturday).

The real orchestration lives in `src.pipeline`. This shim preserves the
--live / --dry-run / --no-email CLI the GitHub Actions workflow already uses,
and maps it to the unified pipeline in `deep` mode.

    uv run python -m src.pipeline_weekly --live      # full deep review, mutates state
    uv run python -m src.pipeline_weekly --dry-run   # preview, no state change
"""

from __future__ import annotations

import argparse
import sys

from src.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly deep review (v2)")
    parser.add_argument("--dry-run", action="store_true", help="Do not mutate state. Default if neither flag passed.")
    parser.add_argument("--live", action="store_true", help="Execute trades and rewrite theses.md.")
    parser.add_argument("--no-email", action="store_true", help="Skip email (still writes a report file).")
    args = parser.parse_args()
    if args.live and args.dry_run:
        parser.error("--live and --dry-run are mutually exclusive")
    dry_run = not args.live  # default = safe
    return run(deep=True, dry_run=dry_run, send_email_flag=not args.no_email)


if __name__ == "__main__":
    sys.exit(main())
