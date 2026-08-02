"""
Consumer sentiment collector: University of Michigan Consumer Sentiment
Index, via the FRED API (free, requires a key - reuses FRED_API_KEY, the
same one used by rates.py).

FRED series UMCSENT is monthly (index, 1966Q1=100, not seasonally adjusted).
UMich releases a mid-month preliminary reading and a final reading at
month-end; FRED tags the observation with the 1st of the survey month, not
the release date. That means this tile will *always* look "old" relative to
a daily run - that's expected, not a bug, and the dashboard always shows the
observation month next to the reading so it's never mistaken for a live
number (same "as of" pattern used for FOMO Fragility's margin-debt axis).

If FRED_API_KEY is not set, this returns a FAILED envelope rather than
guessing - "never carry forward an unlabeled value" per the blueprint.
"""

import os
import requests

from src.envelope import Envelope, now_utc_iso

FRED_SERIES_ID = "UMCSENT"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
MODULE = "consumer_sentiment"


def _fetch_series(api_key: str, limit: int = 14):
    params = {
        "series_id": FRED_SERIES_ID,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    resp = requests.get(FRED_URL, params=params, timeout=30)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    # FRED marks missing/not-yet-published months with "."
    return [o for o in obs if o.get("value") not in (None, ".")]


def collect() -> Envelope:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return Envelope(
            module=MODULE,
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="FRED_API_KEY not set - see module docstring for how to get one.",
        )

    try:
        obs = _fetch_series(api_key)
    except Exception as exc:
        return Envelope(
            module=MODULE,
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"FRED request failed: {exc}",
        )

    if not obs:
        return Envelope(
            module=MODULE,
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="no usable UMCSENT observations returned by FRED",
        )

    latest = float(obs[0]["value"])
    observation_date = obs[0]["date"]

    change_mom = None
    if len(obs) >= 2:
        change_mom = round(latest - float(obs[1]["value"]), 1)

    change_yoy = None
    if len(obs) >= 13:
        change_yoy = round(latest - float(obs[12]["value"]), 1)

    payload = {
        "level": latest,
        "change_mom": change_mom,
        "change_yoy": change_yoy,
        "as_of_month": observation_date[:7],  # YYYY-MM
    }

    return Envelope(
        module=MODULE,
        source="FRED",
        retrieved_at=now_utc_iso(),
        observation_date=observation_date,
        status="EOD",
        payload=payload,
        notes=(
            "UMich Consumer Sentiment is monthly and released with a lag "
            "(mid-month prelim, month-end final) - this reading is always "
            "at least several days to a few weeks old, by design."
        ),
    )
