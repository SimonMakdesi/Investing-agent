# Investing Agent

Autonomous AI-driven Swedish stock research and paper-trading agent. See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full vision and [CLAUDE.md](CLAUDE.md) for the agent's constitution.

**Status:** Phase 1 — foundation / plumbing. No Claude calls yet, no trading decisions yet.

## Setup (local)

1. Install [uv](https://docs.astral.sh/uv/) (already installed on this machine).
2. Install dependencies:
   ```powershell
   uv sync
   ```
3. Copy `.env.example` to `.env` and fill in the keys you have:
   ```powershell
   copy .env.example .env
   ```
   Then edit `.env` and put in:
   - `ANTHROPIC_API_KEY` — from console.anthropic.com (not used until Phase 2)
   - `GMAIL_ADDRESS` — your Gmail address
   - `GMAIL_APP_PASSWORD` — the 16-char app password
   - `BORSDATA_API_KEY` — leave empty until Börsdata approves (Phase 2)

## Phase 1 smoke tests

Run each of these and verify they work:

```powershell
# Create the starting portfolio (100k SEK cash)
uv run python scripts/init_portfolio.py

# Fetch real Swedish stock prices
uv run python scripts/smoke_test_prices.py

# Fetch real insider transactions from Finansinspektionen
uv run python scripts/smoke_test_insiders.py

# Send a test email to yourself
uv run python scripts/smoke_test_email.py

# Run the unit tests
uv run pytest
```

If all five succeed, Phase 1 is verified and we can move to Phase 2.

## Repo layout

```
CLAUDE.md                    Constitution — read by every Claude role
PROJECT_BRIEF.md             The original vision document
prompts/                     Role-specific prompts (added in Phase 2)
src/
  config.py                  Env vars, paths, constants
  data/                      Data source integrations
  portfolio.py               Paper portfolio: load, save, buy, sell
  reporting.py               Email delivery via Gmail SMTP
  claude_client.py           Anthropic API wrapper (stub in Phase 1)
state/
  portfolio.json             Current holdings + cash (source of truth)
  theses.md                  Living journal — agent's memory between cycles
  transactions.log           Append-only trade history
  dossiers/                  One file per watchlist company
reports/                     Weekly reports
archive/                     Raw source material
scripts/                     Manual smoke tests + utilities
tests/                       Unit tests
.github/workflows/
  weekly.yml                 Sunday 18:00 Stockholm — full cycle
  daily.yml                  Mon-Fri — light event monitor
```

## How the agent will run (preview)

Once in Phase 3, the agent runs on its own:

- Every **Sunday evening**, GitHub Actions executes the weekly pipeline: refresh data → screen → analyze → decide → update portfolio → write report → commit → email.
- Every **weekday evening**, a lighter daily cycle scans for material events (earnings, big insider trades, profit warnings) on held positions and the watchlist.
- You receive an email on Sunday. You read it. You do nothing else.

The repo is both the code and the agent's memory. Every weekly run produces commits that document what the agent saw, thought, and decided.
