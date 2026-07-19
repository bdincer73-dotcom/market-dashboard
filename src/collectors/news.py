"""
Stock news collector: Finnhub free "company-news" endpoint, one call per
watchlist ticker (config/watchlist.yaml).

If FINNHUB_API_KEY is missing, this returns FAILED for the whole module (no
guessing). If the key is present but an individual ticker can't be resolved
(e.g. "DRAM"/"BRR" aren't real exchange symbols), that ticker is marked
UNAVAILABLE within the payload rather than silently dropped or fabricated -
so a bad symbol in the watchlist is visible on the dashboard, not hidden.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml
from pathlib import Path

from src.envelope import Envelope, now_utc_iso

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
WATCHLIST_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist.yaml"


def load_watchlist() -> dict:
    with open(WATCHLIST_PATH) as f:
        return yaml.safe_load(f)


def _fetch_ticker_news(ticker: str, api_key: str, lookback_days: int, max_headlines: int):
    today = datetime.now(timezone.utc).date()
    frm = today - timedelta(days=lookback_days)
    params = {
        "symbol": ticker,
        "from": frm.isoformat(),
        "to": today.isoformat(),
        "token": api_key,
    }
    resp = requests.get(FINNHUB_URL, params=params, timeout=20)
    if resp.status_code == 429:
        raise RuntimeError("rate limited")
    resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list):
        # Finnhub returns {} or an error object for unresolvable/invalid symbols.
        return None
    items = sorted(items, key=lambda x: x.get("datetime", 0), reverse=True)
    headlines = []
    for it in items[:max_headlines]:
        dt = it.get("datetime")
        headlines.append({
            "headline": it.get("headline"),
            "source": it.get("source"),
            "url": it.get("url"),
            "published_at": (
                datetime.fromtimestamp(dt, tz=timezone.utc).isoformat(timespec="minutes")
                if dt else None
            ),
        })
    return headlines


def collect() -> Envelope:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return Envelope(
            module="news",
            source="Finnhub",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="FINNHUB_API_KEY not set - get a free key at https://finnhub.io/register",
        )

    wl = load_watchlist()
    tickers = list(wl.get("positions", [])) + list(wl.get("watch_only", []))
    lookback_days = wl.get("news", {}).get("lookback_days", 3)
    max_headlines = wl.get("news", {}).get("max_headlines_per_ticker", 3)

    results = {}
    unavailable = []
    for ticker in tickers:
        try:
            headlines = _fetch_ticker_news(ticker, api_key, lookback_days, max_headlines)
        except Exception as exc:
            results[ticker] = {"status": "UNAVAILABLE", "headlines": [], "reason": str(exc)}
            unavailable.append(ticker)
            time.sleep(0.5)
            continue
        if headlines is None:
            results[ticker] = {"status": "UNAVAILABLE", "headlines": [], "reason": "symbol not resolvable"}
            unavailable.append(ticker)
        else:
            results[ticker] = {"status": "OK", "headlines": headlines, "reason": None}
        time.sleep(0.5)  # be polite to the free-tier rate limit

    ok_count = sum(1 for r in results.values() if r["status"] == "OK")
    status = "FAILED" if ok_count == 0 else "EOD"

    payload = {
        "tickers": results,
        "ok_count": ok_count,
        "unavailable_count": len(unavailable),
        "unavailable_tickers": unavailable,
    }

    return Envelope(
        module="news",
        source="Finnhub",
        retrieved_at=now_utc_iso(),
        observation_date=datetime.now(timezone.utc).date().isoformat(),
        status=status if status != "FAILED" else "FAILED",
        payload=payload if status != "FAILED" else {},
        notes="" if not unavailable else f"unresolvable/rate-limited tickers: {unavailable}",
    )
