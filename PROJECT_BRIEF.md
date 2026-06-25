# Project Brief: Autonomous Stock Analyst Agent

> ## ⚠️ v2 AMENDMENT (2026-06-25) — read this first
>
> The original brief below described a **cautious, benchmark-tracking Swedish paper portfolio**. The owner has since pivoted to a **v2 "aggressive" mandate**. Where this brief and the v2 amendment disagree, **the constitution (`CLAUDE.md`) is canonical** and v2 wins. The historical text is kept for context only.
>
> **What changed in v2:**
> - **Goal flipped:** from "perform comparably to OMXS30 at moderate risk" → **actively chase +50% / 6 months** as a north-star target. It is fake money and a capability test; large drawdowns are acceptable. (§2, §13 below are superseded.)
> - **Markets:** Swedish market **+ the US market**, treated as **one universe with no home bias**. Swedish names auto-pulled across all caps; US widened from a handful of mega-caps to a broad **liquidity-floored** list. (§1, §5 below extended.)
> - **Risk model collapsed to one book** (no Core/Aggressive sleeves). New hard caps: **30% max single holding, 40% max sector, ~8 max holdings, ≥5% cash, no minimum holding period, no minimum holding count.** Still **long-only — no leverage, shorting, or derivatives** (hard line). (§8 below superseded.)
> - **Capital rotation is first-class:** sell a good-but-not-best holding to fund a better idea, not only on thesis break.
> - **Agents redesigned from 5 roles → 4** with a cheap→expensive escalation ladder:
>   - **Scout** (Haiku) — daily, always. Merges the old Screener + Event Monitor. Scans the full US+SE universe **and the current book**, surfacing buy *and* sell/rotation candidates. The cost gate.
>   - **Analyst** (Opus) — daily, conviction-gated, **up to 8 names/day**. Deep note per surfaced name.
>   - **Trader** (Opus) — daily when candidates exist. Merges the old Portfolio Manager + Daily PM. Buys/sells/rotates against the target pace; sees the whole book.
>   - **Journal Keeper** (Haiku) — daily append + weekly rewrite. The agent's private structured memory, not a report.
> - **Cadence:** full decision cycle **every weekday at 22:00 UTC** (after the US close, so both markets get same-day prices) + a **Saturday deep review** (full-universe re-sweep + whole-book rotation + target re-pace).
> - **Reporting:** terse. The agent **no longer writes rationale essays for the owner** — reports show what it did, position sizes, and P&L vs the +50% pace line. A one-line internal rationale/sell-trigger is kept only for the agent's own memory.
> - **Cost / keys:** no new API keys (same Anthropic + Börsdata-Global + yfinance + FI insider + Gmail stack). Roughly $15–30/mo; universe width is nearly free, Opus firing is the cost lever.
>
> Everything from "## 1. Vision" down is the **original v1 brief**, retained for history.

---

## 1. Vision

Build an autonomous, AI-driven investment research and paper-trading agent that runs on a weekly schedule. The agent monitors the Swedish stock market, identifies promising opportunities (stocks, funds, upcoming IPOs), and manages a simulated portfolio to demonstrate its performance over time.

The goal is **not** to beat the market by 30% per year. The goal is to build a transparent, disciplined, AI-managed portfolio whose decisions are reasoned, traceable, and accountable — and that performs comparably to or slightly better than OMXS30 over a 12+ month horizon at reasonable risk.

This is a **personal project for a single user (the project owner)**. It is not a product, has no other users, gives no investment advice to anyone else, and is therefore outside the scope of Finansinspektionen's licensing requirements for investment advisory services.

## 2. The User

- Passive investor — wants to observe and learn, not actively trade
- Comfortable with technology but not a quant
- Lives in Sweden, focuses exclusively on the Swedish market
- Risk profile: **moderate** — wants meaningful returns but not reckless. "Cautious but not so safe it takes 10 years to make 10%."
- Wants transparency in reasoning above all else
- Will read a weekly report on Sunday evenings

## 3. Core Architecture

### Execution model
- **Runs autonomously in GitHub Actions** on a schedule
- **Weekly main cycle**: Sunday evening (~18:00 Europe/Stockholm)
- **Daily light cycle**: short check Mon-Fri for major events (earnings releases, large insider trades, profit warnings on held positions). Flags only — no trades execute outside the Sunday cycle.
- The user does not press any button. The agent operates on its own.

### Code & state
- **Everything lives in a private GitHub repo.** Nothing is stored on the user's local machine.
- The repo is both the code and the agent's persistent memory. Every weekly run produces commits that document what the agent saw, thought, and decided.
- Secrets (API keys, mail credentials) live in GitHub Secrets.

### Agent design: roles, not monolith
The agent is **not** a single Claude call that does everything. It is a pipeline of Claude calls, each with a specialized role and focused context. All roles share a common constitution (the project's `CLAUDE.md`) but each role has its own task-specific prompt.

Recommended roles:
- **Screener** — given a universe of stocks with compressed metrics, picks 5-7 candidates worth deeper analysis this week
- **Analyst** — given one company's dossier and recent material, produces a research note
- **Portfolio Manager** — given the analyses, current portfolio, journal, and risk rules, makes the week's trading decisions
- **Journal Keeper** — updates the running `theses.md` based on the week's events and decisions
- **Event Monitor** (daily) — scans for material events on held positions and watchlist

Roles communicate via files on disk, not via in-memory state. The Portfolio Manager reads what the Analyst wrote, not the raw data the Analyst processed.

## 4. Context Discipline (Critical)

**The single most important design principle: never overwhelm Claude with raw data or full history.** Claude has no memory between calls. Long context degrades reasoning quality. The agent must aggressively process, compress, and discard.

### Rules
- Raw source material (quarterly reports, news articles, prospectuses) is read **once** by an Analyst call, then compressed into a short dossier. Raw data goes to `archive/` and is never sent in routine calls.
- Price data and fundamentals are never sent as raw time series. The code computes derived signals (momentum, volatility, distance from moving averages, valuation vs. sector) and sends only those.
- Each Claude call receives only what is relevant to *that decision*. The Screener doesn't need company dossiers. The Analyst doesn't need the full portfolio. The Portfolio Manager doesn't need raw news feeds.

### The Journal (`theses.md`) — the agent's living memory
At the end of every weekly cycle, the Journal Keeper updates a short markdown file (1-2 pages max) containing:
- Overall market view
- Current thesis per holding ("I own X because Y, I will sell if Z")
- Watchlist with brief rationale per name
- Lessons learned / open questions

The next weekly cycle **starts** by reading this journal. This is how the agent has continuity without bloated context. The agent is forced to distill its own thinking. Old theses that no longer hold are written out of the journal.

### Company dossiers
One short file per company on the watchlist (~500 words):
- Current thesis
- Latest quarterly report summary
- Key risks
- Recent insider activity, news sentiment summary

Updated incrementally — when a new report drops, the Analyst rewrites the relevant section, not the whole file.

## 5. Data Sources

The implementation should integrate (in priority order):

**Essential**
- **Börsdata API** (~150 kr/month) — fundamentals, historical financials, the best Swedish data source
- **Yahoo Finance** (free, via `yfinance` or similar) — price data for `.ST` tickers, daily updates
- **Finansinspektionen insynsregister** (free, daily updates) — insider transactions. Highly informative on Small/Mid Cap. Filter on buys ≥ ~500,000 SEK by default.
- **Nasdaq Stockholm listings** — IPO calendar and prospectuses

**Important**
- **News feeds** via RSS: Placera, Dagens Industri, Affärsvärlden, Redeye (free analyses on small/mid cap)
- **Spotlight Stock Market & NGM** — IPO pipelines beyond Nasdaq main list
- **Bolagsverket** — annual reports for non-listed comparisons if needed

**Optional / later**
- Reddit, X/Twitter sentiment (low priority for Swedish market — limited signal)
- Google Trends
- Job posting feeds (signal of growth)

## 6. Weekly Cycle (Sunday)

The pipeline should look roughly like this. Implementation details are open.

1. **Data refresh** — pull new prices, news, insider transactions, any new quarterly reports or prospectuses since last run
2. **Material ingestion** — for each new long document (report, prospectus), Analyst role produces a dossier entry; raw content archived
3. **Universe assembly** — compile compressed metrics for the relevant universe (e.g. OMX Large Cap + Mid Cap + curated Small Cap watchlist + active First North names)
4. **Screening** — Screener picks 5-7 names for deeper look this week
5. **Deep analysis** — Analyst role produces one research note per shortlisted name
6. **Decision** — Portfolio Manager reads journal + portfolio + analyses, decides buys/sells/holds, respecting risk rules
7. **Execution** — code updates the paper portfolio, logs the transactions
8. **Journal update** — Journal Keeper revises `theses.md`
9. **Report generation** — weekly markdown report emailed to user
10. **Commit & push** — everything versioned in GitHub

Daily cycles are much simpler: refresh prices, check for major events on holdings/watchlist, flag if needed.

## 7. Suggested Repo Structure

The implementation can adjust this, but as a starting point:

```
/
├── CLAUDE.md                    # Constitution: identity, philosophy, rules
├── prompts/
│   ├── screener.md
│   ├── analyst.md
│   ├── portfolio_manager.md
│   ├── journal_keeper.md
│   └── event_monitor.md
├── src/                         # Python code
│   ├── pipeline_weekly.py
│   ├── pipeline_daily.py
│   ├── data/                    # Data source integrations
│   ├── claude_client.py         # Wrapper around Anthropic API
│   ├── portfolio.py             # Paper portfolio logic
│   └── reporting.py             # Email report generation
├── state/
│   ├── portfolio.json           # Current holdings, cash
│   ├── theses.md                # Living journal
│   ├── transactions.log         # Full trade history
│   └── dossiers/                # One file per watchlist company
├── reports/                     # Weekly reports, one per file
├── archive/                     # Raw source material
├── .github/workflows/
│   ├── weekly.yml
│   └── daily.yml
└── README.md
```

## 8. Risk & Allocation Rules (Hard Constraints)

The portfolio is divided into two sleeves with different rules. These are non-negotiable and must be enforced in code, not just in prompts.

### Core sleeve (~80% of portfolio)
The disciplined, quality-focused majority of the portfolio. Conservative allocation rules apply:

- **Max 70% in equities within this sleeve**, minimum 30% cash buffer (of core sleeve)
- **Max 15% in any single holding** (of total portfolio)
- **Max 25% in any single sector** (of total portfolio)
- **Minimum 4 holdings** when fully invested
- Focus on quality businesses with strong cash flows and proven profitability

### Aggressive sleeve (up to 20% of portfolio)
A dedicated "high conviction" budget the Portfolio Manager may deploy on riskier, higher-upside opportunities. This is where the agent is allowed — even encouraged — to take meaningful positions when the thesis is strong.

- **Max 20% of total portfolio** allocated here at any time
- May include: Small Cap, First North names, IPOs (after first earnings report), turnaround cases, thematic bets, sector concentration
- **Max 10% of total portfolio in any single aggressive position** (so 2 max-sized aggressive bets, or several smaller ones)
- Positions here must still have a written, defensible thesis — aggressive ≠ random
- May be 0% if no compelling opportunities exist. Don't force trades into this sleeve.
- Aggressive positions count toward the overall single-holding and sector caps above

### Universal rules (both sleeves)
- **Total portfolio**: 50,000 - 100,000 SEK (configurable at start)
- **Total equity exposure across both sleeves**: max ~90% (so always at least ~10% cash on hand for opportunities and buffers)
- **Max ~10 holdings total** (avoid index-with-extra-steps)
- **No leverage, no derivatives, no shorting** — long-only cash account
- **No day trading** — minimum holding period 4 weeks unless thesis demonstrably breaks
- The Portfolio Manager must explicitly label each position as Core or Aggressive in the journal, and report sleeve allocation in every weekly report

## 9. Investment Philosophy (Soft Guidance for Prompts)

These should be encoded in the constitution and in role prompts:

- Prefer quality businesses (strong cash flows, proven profitability, reasonable debt) over story stocks
- Insider buying is a strong signal, especially on Small/Mid Cap. Insider selling is weaker but worth noting.
- Sell when the thesis breaks, not when the price moves. Avoid selling into panic, buying into euphoria.
- When uncertain, hold cash. "No action" is a valid decision.
- Never chase. If a move was missed, it was missed.
- Diversification across sectors is mandatory; across styles (growth/value/dividend) is preferred.
- The Core sleeve (~80%) is for quality and discipline. The Aggressive sleeve (up to 20%) is where higher-conviction, higher-risk bets belong — and they should be meaningful when taken, not symbolic. A 1% position in an aggressive bet is wasted budget. Either take a real position (5-10%) or skip it.
- That said, the Aggressive sleeve should never be deployed just because it's available. An empty aggressive sleeve is fine. Forced trades are worse than no trades.
- Benchmark against OMXS30. The goal is risk-adjusted outperformance over 12+ months.
- Realistic expectation: index ± a few percent per year. If results look much better than that in early months, suspect overfitting or luck, not skill.

## 10. The Weekly Report

What the user receives by email on Sunday evenings. Should include:

- **Headline summary** (2-3 sentences): what changed, what was decided
- **Portfolio status**: total value, cash %, return vs OMXS30 since inception and this week
- **This week's decisions**: buys/sells with the reasoning in the Portfolio Manager's own words
- **Holdings overview**: each position with current thesis status (intact / weakening / broken)
- **Watchlist movement**: additions, removals, notable changes in conviction
- **Flagged events**: anything material that happened during the week
- **The journal entry** for this week, in full

Transparency is the point. The user must always be able to answer "why did the AI do this?"

## 11. What's Open for Claude Code to Design

The brief above sets the *what* and *why*. The following implementation choices are open and Claude Code should propose them:

- Exact Python libraries and project structure
- Schema for `portfolio.json`, dossier format, transaction log format
- Specific data refresh strategy (incremental vs. full)
- How to handle Börsdata API rate limits and caching
- Email delivery mechanism (Mailgun, SendGrid, SMTP)
- Backtesting framework for validation (later phase)
- Exact prompt wording for each role (drafted from the principles in this brief)
- Error handling and recovery (what happens if a data source is down on Sunday?)
- Tooling for tool-use / function-calling within Claude calls (e.g. letting an Analyst request historical data on demand)

## 12. Build Order

Suggested phasing — don't build everything at once.

**Phase 1: Foundation (week 1-2)**
- Repo setup, GitHub Actions skeleton, secrets management
- Data layer: Börsdata + yfinance + FI insider feed working
- Paper portfolio data model + transaction log
- Simple email delivery

**Phase 2: Single weekly cycle (week 2-3)**
- `CLAUDE.md` constitution drafted
- All five role prompts drafted
- Weekly pipeline end-to-end, even if rough
- First simulated weekly run, with the user reviewing output manually before pushing live

**Phase 3: Autonomy (week 3-4)**
- Schedule live, agent runs unattended
- Daily light cycle added
- Refinement loop: weekly review of how the prompts performed, adjust

**Phase 4: Depth (month 2+)**
- IPO pipeline integration
- Backtesting framework
- Tool-use for on-demand historical lookups
- Sector and style diversification analytics

## 13. Success Criteria

This project is successful if, after 6 months:

1. The agent has run autonomously every week without manual intervention
2. Every decision is traceable to a documented rationale in the journal and reports
3. The risk rules have never been violated
4. The user has learned more about the Swedish market by reading the agent's reasoning
5. Returns are within a reasonable range vs. OMXS30. With a ~20% aggressive sleeve, larger deviations both up and down are expected. Anything from -10% to +15% relative is normal; outside that range warrants investigation (overfitting, luck, or rule violations).

Failure is not "underperforming the index by a couple of percent." Failure is "the agent made decisions I can't understand, or violated its own rules, or stopped running."

---

*End of brief. Hand this to Claude Code and let it ask clarifying questions before writing any code.*
