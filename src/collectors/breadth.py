"""
Breadth collector.

Primary: compute % of current S&P 500 constituents trading above their 20/50/200
day SMA, from adjusted daily closes (yfinance, free, no key).

The blueprint calls for reconciling this against a licensed/broker breadth feed
(thinkorswim $SPXA20R etc.). We don't have broker API access in R1, so that
reconciliation gate is stubbed as UNAVAILABLE rather than skipped silently -
see notes in the returned envelope.

Reject <95% symbol coverage per the blueprint's quality gate.
"""

import io
import pandas as pd
import requests

from src.envelope import Envelope, now_utc_iso

WIKI_SPX_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MIN_COVERAGE_PCT = 95.0
HISTORY_DAYS = "400d"  # enough trading days to compute a trailing 200D SMA


def get_constituents() -> list[str]:
    """Pull the current S&P 500 ticker list from Wikipedia. Falls back to
    raising if the page structure changes - caller treats that as FAILED."""
    resp = requests.get(
        WIKI_SPX_URL,
        headers={"User-Agent": "Mozilla/5.0 (market-dashboard research bot)"},
        timeout=30,
    )
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]
    tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    return sorted(set(tickers))


def collect(_tickers_override: list[str] | None = None) -> Envelope:
    """`_tickers_override` is for tests only - lets us verify the SMA/coverage
    logic against a small ticker subset without downloading all ~500 names."""
    import yfinance as yf

    try:
        constituents = _tickers_override if _tickers_override is not None else get_constituents()
    except Exception as exc:
        return Envelope(
            module="breadth",
            source="wikipedia+yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"could not fetch constituent list: {exc}",
        )

    try:
        raw = yf.download(
            tickers=constituents,
            period=HISTORY_DAYS,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        return Envelope(
            module="breadth",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"bulk download failed: {exc}",
        )

    above_20 = above_50 = above_200 = 0
    usable = 0
    observation_date = None

    for ticker in constituents:
        try:
            closes = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(closes) < 200:
            continue
        usable += 1
        last_close = closes.iloc[-1]
        sma20 = closes.rolling(20).mean().iloc[-1]
        sma50 = closes.rolling(50).mean().iloc[-1]
        sma200 = closes.rolling(200).mean().iloc[-1]
        if last_close > sma20:
            above_20 += 1
        if last_close > sma50:
            above_50 += 1
        if last_close > sma200:
            above_200 += 1
        candidate_date = closes.index[-1]
        if observation_date is None or candidate_date > observation_date:
            observation_date = candidate_date

    coverage_pct = round(100 * usable / len(constituents), 1) if constituents else 0.0

    if coverage_pct < MIN_COVERAGE_PCT:
        return Envelope(
            module="breadth",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=str(observation_date.date()) if observation_date is not None else None,
            status="FAILED",
            payload={},
            notes=(
                f"symbol coverage {coverage_pct}% below the {MIN_COVERAGE_PCT}% "
                "quality gate - rejecting this run's breadth numbers."
            ),
        )

    payload = {
        "pct_above_20": round(100 * above_20 / usable, 1),
        "pct_above_50": round(100 * above_50 / usable, 1),
        "pct_above_200": round(100 * above_200 / usable, 1),
        "constituent_count": len(constituents),
        "usable_count": usable,
        "coverage_pct": coverage_pct,
        # deltas vs prior run are filled in by src/store.py, which has history.
    }

    return Envelope(
        module="breadth",
        source="yfinance (constituents: wikipedia)",
        retrieved_at=now_utc_iso(),
        observation_date=str(observation_date.date()) if observation_date is not None else None,
        status="EOD",
        payload=payload,
        notes=(
            "Not reconciled against a licensed breadth feed ($SPXA20R/50R/200R) - "
            "R1 relies on self-computed breadth only. Add a broker feed in R2/R3."
        ),
    )
