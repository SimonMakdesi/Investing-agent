# Journal — Investing Agent

## 1. Market view

Swedish large-caps broadly weak over the past three months, with industrials and defensives 8–15% off highs. The dominant signal this week was a cluster of very large insider purchases — most notably the Persson family in H&M and a board-level individual in Essity — suggesting insiders see value in the pullback. Constructive on quality consumer names at current prices; happy to hold 90% cash and build slowly as conviction is earned. No rush to deploy.

## 2. Holdings

**HM-B.ST** — Core | Consumer Discretionary | opened 2026-05-24
- **Thesis:** Controlling-family member (Karl-Johan Persson) bought ~1.2bn SEK across six transactions in eight days into an 11% drawdown. Among the strongest insider signals possible on a Swedish Large Cap. Starter position pending fundamental confirmation.
- **Sell if:** Profit warning, gross margin collapse, or confirmed structural market-share loss to Shein/Temu — not on price weakness alone.
- **Status:** `intact`

**ESSITY-B.ST** — Core | Consumer Staples | opened 2026-05-24
- **Thesis:** Cluster of insider buying led by ~190M SEK from one board-level individual (Åberg) across two dates, on a defensive hygiene/staples name sitting on its 200-day MA after an 8% drawdown. Steady cash-flow profile fits Core sleeve.
- **Sell if:** Sustained margin deterioration (input costs structurally above pricing power), or insider thesis contradicted by a poor quarterly print.
- **Status:** `intact`

## 3. Watchlist

**THULE.ST** — Board insider (Blomquist) bought ~22M SEK over eight days in a quality outdoor brand trading 22% off highs. Passed this week due to elevated volatility (37%) and single-insider signal. Revisit on next quarterly print or continued insider follow-through; entry interest around 230 SEK.

**SOBI.ST** — Structurally attractive rare-disease pharma with a strong +43% 12-month trend. Screener mis-read paired option-exercise transactions as a buy — actual net is a disposal. No insider signal; not actionable at 52-week highs. Revisit on a 10–15% pullback with independent fundamental confirmation; interest around 380 SEK.

**NCC-B.ST** — Cluster of genuine open-market CEO and executive buys in May while stock is 17% off highs. Cannot verify order book or project risk. Revisit when fundamental data available or price approaches ~180 SEK.

**BILL.ST (BillerudKorsnäs)** — Mats Qviberg's ~13M SEK personal purchase is a high-quality contrarian signal in a 41%-down cyclical. Most other May insider activity was share-program mechanics (paired buy/sell), not conviction. Blocked by inability to assess balance sheet and dilution risk. Need quarterly numbers and net debt before acting.

**AAK.ST** — Defensive specialty-ingredients compounder, price stable on 200-day MA. Screener flagged as a buy but net insider activity is a sale (executives sold 5–10× what they bought on the same day). No actionable signal now; genuine Core candidate at ~225 SEK on a clean fundamental print.

## 4. Lessons learned / open questions

- **Screener bug — net insider flow:** The screener is summing gross Förvärv rows while ignoring same-day Avyttring, producing false-positive buy signals. Confirmed on SOBI, AAK, and partially on BILL (NCC). Priority fix before next cycle: screener should compute *net* per-insider flow per day.
- **Starter sizing is right for signal-only entries:** Both positions are ~5% of portfolio, taken without fundamental confirmation. This is the correct discipline — act on the signal, stay small until fundamentals confirm or deny.
- **Open question:** When do we get access to quarterly fundamental data (revenue, margins, debt) for portfolio names? H&M and Essity both have pending confirmation needs — flag for Phase 4 data-source build.
- **Persson family vs. typical insider:** Constitution notes insider buying is a "strong signal on Small/Mid Cap." The H&M buy is Large Cap, but at 1.2bn SEK from a controlling family, it arguably supersedes the usual Large Cap discount. Worth keeping this calibration in the lessons for future Large Cap screening.