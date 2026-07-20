# Market Dashboard — R1 (market core) + news + Bear Indicator + R2 (CSP candidates)

Automated regime dashboard from the blueprint: breadth, sector rotation (SPY/RSP
+ 11 sector SPDRs), volatility (VIX/VIX9D/VIX3M/VVIX), and Treasury yields
(2Y/10Y/2s10s), scored into a GREEN / AMBER / RED regime call and published as
a static HTML dashboard. Built per "Python + GitHub Actions + static HTML" —
the recommended v1 path in the blueprint.

Additions on top of R1:
- **Market news** for your watchlist tickers (`config/watchlist.yaml`, sourced
  from your Options P&L Tracker's Open Positions/Cash Collateral/Stock Holdings
  tabs), via Finnhub's free company-news endpoint.
- **Bear Indicator** (weekly only, Saturday run) — ported from the same
  tracker's "Bear Indicator" tab: Chaikin Money Flow on SPY/VOO/IWV/QQQ/RSP,
  breadth, Net New Hi %, and SPY/RSP's position vs their 20/50D SMAs, rolled
  into an 0-8 composite score and a Bearish/Cautionary/Watch/Constructive
  signal. Uses the tracker's original 8-condition formula (see "Known
  limitations" for why, not the newer formula from the 7/11 row).
- **R2 — CSP candidate screening** (daily): earnings calendar joins (Finnhub),
  put option chains (yfinance, free — see caveat below), and the blueprint's
  return/liquidity/event gates. One representative strike per watchlist
  ticker, picked by proximity to a target delta band among strikes that pass
  all gates; tickers that fail a gate still show up in a "watch only" list
  with the specific reason, instead of disappearing.

**R2 scope actually built:** cash-secured put candidates only (no covered
calls yet), screening only (doesn't track your actual open positions'
assignment risk — that's still manual via your tracker). See "Known
limitations" for the option-chain data-quality caveat.

## Pipeline

```
source → validate → normalize → calculate → score → publish → archive
```

- `src/collectors/` — one module per data source (breadth, sectors, volatility, rates, news, bear_indicator, earnings, options_chain)
- `config/watchlist.yaml` — tickers for news and R2 candidate scoring
- `src/envelope.py` — the common shape every collector returns (status LIVE/EOD/STALE/FAILED)
- `src/store.py` — SQLite archive: raw snapshots (immutable) + calculated signals, so every number is traceable and any past dashboard is reproducible
- `src/scoring.py` — regime rules engine, thresholds in `config/thresholds.yaml`
- `src/candidate_scoring.py` — R2 CSP candidate gates (return/liquidity/event), same thresholds file
- `src/publish.py` + `templates/dashboard.html.jinja` — renders `docs/latest.html` (+ `docs/index.html`, identical) and an immutable `docs/archive/<date>-<run_type>.html`. Output lives in `docs/` specifically so GitHub Pages can serve it directly.
- `src/main.py` — orchestrates the full run

## One-time setup

1. **Get a free FRED API key** (for Treasury yields): https://fred.stlouisfed.org/docs/api/api_key.html
   Takes about a minute, no cost. Without this key, the Rates tile reports FAILED
   rather than guessing — the dashboard is designed to be honest about missing
   data, per the blueprint's "never carry forward an unlabeled value" rule.

2. **Push this project to your own GitHub repo:**
   ```bash
   cd market-dashboard
   git init
   git add .
   git commit -m "Initial market dashboard build"
   git branch -M main
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

3. **Get a free Finnhub API key** (for stock news): https://finnhub.io/register
   Also free, no card required.

4. **Add both secrets** in your repo: Settings → Secrets and variables → Actions →
   New repository secret → `FRED_API_KEY` and `FINNHUB_API_KEY`.

5. **Enable Actions** if prompted (Actions tab → "I understand my workflows, go ahead and enable them").

That's it — `.github/workflows/daily.yml` runs weekday mornings, `weekly.yml`
runs Saturdays, and both commit the updated `data/` (SQLite archive) and
`docs/` (dashboard HTML) back to the repo so history accumulates run over run.

## Publishing it to the internet (GitHub Pages)

**Privacy note (read before making the repo public):** R1 alone was pure public
market data. With the news feature added, `config/watchlist.yaml` and the
published dashboard now reveal which tickers you're trading options on
(though not size, strikes, or account values - those still live only in your
local tracker file, never uploaded here). If you'd rather not disclose even
that, keep the repo private and skip Pages (GitHub Pages on the free plan
requires a public repo), or move `config/watchlist.yaml` to a `.gitignore`d
local override before pushing.

If that's fine, you can host it at a public URL for free:

1. Make the repo public: Settings → General → scroll to "Danger Zone" →
   **Change visibility** → Public. (GitHub Pages on the free plan only works
   on public repos — private-repo Pages needs a paid plan.)
2. Settings → **Pages** → under "Build and deployment," Source: **Deploy from
   a branch** → Branch: `main`, folder: **/docs** → Save.
3. Wait a minute, then your dashboard is live at:
   `https://<your-username>.github.io/market-dashboard/`

Every daily/weekly Action run updates `docs/index.html`, so the public URL
always reflects the latest run automatically - no extra deploy step needed.

## Running locally

```bash
cd market-dashboard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FRED_API_KEY=your_key_here     # omit to see the FAILED/honest-gap behavior
python -m src.main --run-type daily
open docs/latest.html                 # macOS; use xdg-open on Linux
```

The breadth collector downloads ~500 tickers' daily history — expect this step
to take a few minutes locally and on the Actions runner.

## Viewing the dashboard

- `docs/latest.html` / `docs/index.html` — always the most recent run (identical content), open directly in a browser or via the public Pages URL
- `docs/archive/YYYY-MM-DD-<daily|weekly>.html` — every past run, never overwritten
- `docs/latest_summary.txt` — plaintext version, e.g. for a Slack/email step you bolt on later
- `data/market_dashboard.db` — SQLite archive of every raw snapshot and calculated signal (open with any SQLite browser, e.g. `sqlite3 data/market_dashboard.db`)

## Tuning the regime rules

Edit `config/thresholds.yaml` — no code changes needed. Comments in that file
explain each threshold (VIX calm band, breadth deterioration deltas, narrow-
leadership rank cutoff, etc.).

## Acceptance test (per the blueprint's R1 "done when")

> 10 consecutive sessions run unattended; no unlabeled stale values.

Once secrets are set and Actions is enabled, let it run for two full weeks
(10 weekdays) unattended and check `docs/archive/` for 10 consecutive dated
files, plus confirm every tile shows an explicit status badge (never a bare
number with no LIVE/EOD/STALE/FAILED label).

## Known limitations (by design, for R1)

- **CME FedWatch** cut/hold/hike distribution is a paid API — the Rates tile
  links to the free public tool instead of pulling the distribution automatically.
- **Breadth** is self-computed from Wikipedia's constituent list + yfinance
  closes, not reconciled against a licensed feed (thinkorswim $SPXA20R/50R/200R).
  A >2pp discrepancy-detection gate from the blueprint isn't wired up because
  there's no second feed yet to compare against.
- **Weekly run** reuses the same daily logic; it doesn't yet add the
  1W sector-rank-migration / Fed-probability-delta view the blueprint describes
  for Saturdays. The sector rank history needed for that is already being
  archived in `raw_snapshots`, so it's a `src/scoring.py` + template addition
  when you're ready for R2, not a data-collection gap.
- **yfinance** is unofficial and occasionally rate-limits bulk requests. If a
  run shows more FAILED tiles than usual, it's most often this — rerun via
  the Actions tab's "Run workflow" button.
- **This is not a broker feed - verify before you trade.** CSP candidate bid/ask,
  open interest, and implied vol come from yfinance's free option chain, which
  can lag real-time and occasionally shows stale or zero quotes. Delta is
  *estimated* via Black-Scholes from that implied vol, not a real broker Greek.
  Treat every candidate as a starting point to check against your actual
  broker quote before entering a trade, not an execution-ready number.
- **R2 covers cash-secured puts only** — no covered-call candidates on your
  actual holdings (CLS, BRR, XLK) yet, and it doesn't track your *existing*
  open positions' assignment risk (cushion %, ITM flags) - that's still your
  tracker's Open Positions tab. This build only screens for *new* CSP entries.
- **Exposure/cash-band logic** (R3: portfolio-level concentration caps, cash
  reserve bands) isn't built - each candidate is scored independently of what
  you already hold or how much collateral is already committed.
- **Candidate picks are one strike per ticker**, chosen by proximity to a
  target delta band (config/thresholds.yaml) among gate-passing strikes, not
  an exhaustive list of every viable strike/expiry combination.
- **Bear Indicator formula**: the tracker used one 8-condition boolean formula
  for its 6/12-7/02 rows, then switched to a differently-scaled continuous
  formula for the 7/11 row while keeping the same 🔴≥6/🟠≥4/🟡≥2 bucket
  thresholds - those don't actually line up under the new formula. This build
  uses the original boolean version throughout, so scores here may not exactly
  match what the 7/11 row in your spreadsheet shows.
- **"DRAM" and "BRR"** in `config/watchlist.yaml` are carried over verbatim
  from the tracker. Neither is a standard resolvable ticker on Finnhub as far
  as this build can tell - expect those two to show as unavailable in the news
  section until you confirm/correct the actual symbols.
- **News** only covers headlines (Finnhub free tier) - no sentiment scoring,
  no filtering by relevance to your specific position (strike/expiry), and
  it's not joined to the earnings calendar yet (that's still R2).
