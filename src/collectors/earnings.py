"""
Earnings calendar collector: Finnhub free "calendar/earnings" endpoint,
one call per watchlist ticker (only the `positions` list - names you'd
actually trade options on, not the Mag 7 watch_only names).

Per the blueprint's event-integrity quality gate: a TBD/unconfirmed date is
risk, not certainty - it's flagged, not silently treated as "no earnings."
If FINNHUB_API_KEY is missing this is FAILED for the whole module, same
honest-gap pattern as every other collector.
"""

import os
from datetime import date, datetime, timedelta

import requests
import yaml
from pathlib import Path

from src.envelope import Envelope, now_utc_iso

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
WATCHLIST_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist.yaml"
LOOKAHEAD_DAYS = 60  # comfortably covers the 30-40 DTE candidate window


def load_watchlist() -> dict:
    with open(WATCHLIST_PATH) as f:
        return yaml.safe_load(f)


def _fetch_ticker_earnings(ticker: str, api_key: str, today: date):
    params = {
        "from": today.isoformat(),
        "to": (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
        "symbol": ticker,
        "token": api_key,
    }
    resp = requests.get(FINNHUB_URL, params=params, timeout=20)
    resp.raise_for_status()
    events = resp.json().get("earningsCalendar", [])
    if not events:
        return None  # no confirmed/estimated report in the lookahead window
    # Finnhub can return more than one row (revisions) - take the soonest.
    events = sorted(events, key=lambda e: e.get("date", "9999-99-99"))
    ev = events[0]
    ev_date = ev.get("date")
    confirmed = ev.get("epsEstimate") is not None or ev.get("hour") in ("bmo", "amc")
    days_to_event = None
    if ev_date:
        try:
            days_to_event = (datetime.strptime(ev_date, "%Y-%m-%d").date() - today).days
        except ValueError:
            pass
    return {
        "date": ev_date,
        "hour": ev.get("hour") or "TBD",
        "confirmed": confirmed,
        "days_to_event": days_to_event,
    }


def collect() -> Envelope:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return Envelope(
            module="earnings",
            source="Finnhub",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="FINNHUB_API_KEY not set - get a free key at https://finnhub.io/register",
        )

    wl = load_watchlist()
    tickers = list(wl.get("positions", []))
    today = date.today()

    results = {}
    errors = []
    for ticker in tickers:
        try:
            ev = _fetch_ticker_earnings(ticker, api_key, today)
        except Exception as exc:
            results[ticker] = {"status": "UNAVAILABLE", "reason": str(exc)}
            errors.append(ticker)
            continue
        if ev is None:
            results[ticker] = {"status": "NONE_SCHEDULED", "event": None}
        else:
            results[ticker] = {"status": "SCHEDULED", "event": ev}

    scheduled_count = sum(1 for r in results.values() if r["status"] == "SCHEDULED")
    status = "FAILED" if scheduled_count == 0 and len(errors) == len(tickers) else "EOD"

    payload = {
        "tickers": results,
        "scheduled_count": scheduled_count,
        "lookahead_days": LOOKAHEAD_DAYS,
        "errors": errors,
    }

    return Envelope(
        module="earnings",
        source="Finnhub",
        retrieved_at=now_utc_iso(),
        observation_date=today.isoformat(),
        status=status if status != "FAILED" else "FAILED",
        payload=payload if status != "FAILED" else {},
        notes="" if not errors else f"errors on: {errors}",
    )
