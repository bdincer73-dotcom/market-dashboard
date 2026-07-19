"""
Treasury yields collector: FRED API (free, requires a key).

If FRED_API_KEY is not set, this returns a FAILED envelope rather than
guessing - "never carry forward an unlabeled value" per the blueprint. Get a
free key at https://fred.stlouisfed.org/docs/api/api_key.html and set it as
the FRED_API_KEY environment variable / GitHub Actions secret.

CME FedWatch (next-meeting cut/hold/hike distribution) is a paid API in the
blueprint's source hierarchy. R1 does not include it - the dashboard links to
the free public CME FedWatch tool instead. Add the paid feed in R2/R3 if you
want the distribution numbers pulled in automatically.
"""

import os
import requests

from src.envelope import Envelope, now_utc_iso

FRED_SERIES = {"DGS2": "us_2y", "DGS10": "us_10y"}
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def _fetch_series(series_id: str, api_key: str, limit: int = 10):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    resp = requests.get(FRED_URL, params=params, timeout=30)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    # FRED marks holidays/missing days with "."
    return [o for o in obs if o.get("value") not in (None, ".")]


def collect() -> Envelope:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return Envelope(
            module="rates",
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="FRED_API_KEY not set - see module docstring for how to get one.",
        )

    values = {}
    observation_date = None
    try:
        for series_id, label in FRED_SERIES.items():
            obs = _fetch_series(series_id, api_key)
            if not obs:
                continue
            latest = float(obs[0]["value"])
            change_1w = None
            if len(obs) >= 6:
                change_1w = round((latest - float(obs[5]["value"])) * 100, 1)  # bps
            change_1d = (
                round((latest - float(obs[1]["value"])) * 100, 1) if len(obs) >= 2 else None
            )
            values[label] = {"level": latest, "change_1d_bps": change_1d, "change_1w_bps": change_1w}
            candidate_date = obs[0]["date"]
            if observation_date is None or candidate_date > observation_date:
                observation_date = candidate_date
    except Exception as exc:
        return Envelope(
            module="rates",
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"FRED request failed: {exc}",
        )

    if "us_2y" not in values or "us_10y" not in values:
        return Envelope(
            module="rates",
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=observation_date,
            status="FAILED",
            payload={},
            notes="one or both Treasury series missing from FRED response",
        )

    spread_2s10s = round((values["us_10y"]["level"] - values["us_2y"]["level"]) * 100, 1)  # bps
    payload = {
        **values,
        "spread_2s10s_bps": spread_2s10s,
        "fed_watch_note": (
            "CME FedWatch distribution not pulled automatically in R1 (paid API). "
            "See https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
        ),
    }

    return Envelope(
        module="rates",
        source="FRED",
        retrieved_at=now_utc_iso(),
        observation_date=observation_date,
        status="EOD",
        payload=payload,
    )
