# Market Dashboard — R1 (market core)

Automated regime dashboard from the blueprint: breadth, sector rotation (SPY/RSP
+ 11 sector SPDRs), volatility (VIX/VIX9D/VIX3M/VVIX), and Treasury yields
(2Y/10Y/2s10s), scored into a GREEN / AMBER / RED regime call and published as
a static HTML dashboard. Built per "Python + GitHub Actions + static HTML" —
the recommended v1 path in the blueprint.

**Scope note:** this is R1 only — the "reliable market core." Earnings joins,
your options watchlist, and CME FedWatch's paid probability feed are R2/R3 and
are not included yet (see "Known limitations" below).

## Pipeline

```
source → validate → normalize → calculate → score → publish → archive
```

- `src/collectors/` — one module per data source (breadth, sectors, volatility, rates)
- `src/envelope.py` — the common shape every collector returns (status LIVE/EOD/STALE/FAILED)
- `src/store.py` — SQLite archive: raw snapshots (immutable) + calculated signals, so every number is traceable and any past dashboard is reproducible
- `src/scoring.py` — rules engine, thresholds in `config/thresholds.yaml`
- `src/publish.py` + `templates/dashboard.html.jinja` — renders `public/latest.html` and an immutable `public/archive/<date>-<run_type>.html`
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

3. **Add the secret** in your repo: Settings → Secrets and variables → Actions →
   New repository secret → name `FRED_API_KEY`, value from step 1.

4. **Enable Actions** if prompted (Actions tab → "I understand my workflows, go ahead and enable them").

That's it — `.github/workflows/daily.yml` runs weekday mornings, `weekly.yml`
runs Saturdays, and both commit the updated `data/` (SQLite archive) and
`public/` (dashboard HTML) back to the repo so history accumulates run over run.

## Running locally

```bash
cd market-dashboard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FRED_API_KEY=your_key_here     # omit to see the FAILED/honest-gap behavior
python -m src.main --run-type daily
open public/latest.html               # macOS; use xdg-open on Linux
```

The breadth collector downloads ~500 tickers' daily history — expect this step
to take a few minutes locally and on the Actions runner.

## Viewing the dashboard

- `public/latest.html` — always the most recent run, open directly in a browser
- `public/archive/YYYY-MM-DD-<daily|weekly>.html` — every past run, never overwritten
- `public/latest_summary.txt` — plaintext version, e.g. for a Slack/email step you bolt on later
- `data/market_dashboard.db` — SQLite archive of every raw snapshot and calculated signal (open with any SQLite browser, e.g. `sqlite3 data/market_dashboard.db`)

## Tuning the regime rules

Edit `config/thresholds.yaml` — no code changes needed. Comments in that file
explain each threshold (VIX calm band, breadth deterioration deltas, narrow-
leadership rank cutoff, etc.).

## Acceptance test (per the blueprint's R1 "done when")

> 10 consecutive sessions run unattended; no unlabeled stale values.

Once secrets are set and Actions is enabled, let it run for two full weeks
(10 weekdays) unattended and check `public/archive/` for 10 consecutive dated
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
- **Earnings risk and the CSP/covered-call watchlist** are entirely R2 — no
  candidate scoring, no options data, no exposure/cash-band logic yet.
- **yfinance** is unofficial and occasionally rate-limits bulk requests. If a
  run shows more FAILED tiles than usual, it's most often this — rerun via
  the Actions tab's "Run workflow" button.
