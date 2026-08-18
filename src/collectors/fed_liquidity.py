"""
Fed Liquidity collector: FRED API (free, requires FRED_API_KEY - same key
already used by rates.py and consumer_sentiment.py; see rates.py's docstring
for how to get one).

Tracks the "Fed net liquidity" macro overlay used for CSP sizing: how much
cash the Fed + Treasury + the ON RRP facility are draining from, or adding
to, the financial system - independent of what breadth/technicals say. A
Contracting reading can justify smaller/safer CSP sizing even when the
regime call and breadth both look fine, on the theory that liquidity leads
and breadth eventually follows.

Series (all FRED, all "Wednesday level" weekly prints except ON RRP which is
daily):
  WALCL      - Fed total assets (H.4.1 balance sheet). Millions of $, weekly.
  WTREGEN    - Treasury General Account balance. Billions of $, weekly.
  RRPONTSYD  - Overnight reverse repo facility usage. Billions of $, daily -
               matched to each WALCL/WTREGEN Wednesday date (nearest prior
               daily print, in case RRP's own series has a gap on a date the
               H.4.1 still published for).
  WRESBAL    - Bank reserves held at the Fed. Billions of $, weekly. Spec'd
               as "when available" - fetched best-effort; a WRESBAL miss
               degrades the reading (reserves fields are None, and the
               scoring module drops the reserves term) rather than failing
               the whole envelope, since WALCL/WTREGEN/RRP alone are enough
               to compute Net Liquidity.

Net Liquidity = Fed Assets - TGA - ON RRP. This is the widely used proxy,
NOT an official Fed metric - it mixes a Fed-driven number (WALCL) with two
Treasury/money-market-driven numbers (TGA, RRP) that can move against the
Fed's own QT/QE stance. A TGA-driven drain in particular can reverse fast
once Treasury spends the cash back out, which is why the scoring module
calls that distinction out explicitly rather than treating every
Contracting print the same. Bank reserves are surfaced alongside as a
second, arguably "cleaner" Fed-driven gauge for the same reason.

Weekly-only cadence, same reasoning and carry-forward pattern as
bear_indicator.py / fomo_fragility.py - main.py only calls collect() on
`--run-type weekly` runs.
"""

import os
from datetime import datetime

import requests

from src.envelope import Envelope, now_utc_iso

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# label -> (FRED series id, divisor to get $ billions)
REQUIRED_SERIES = {
    "fed_assets": ("WALCL", 1000.0),   # millions -> billions
    "tga": ("WTREGEN", 1.0),           # already billions
}
RESERVES_SERIES_ID = "WRESBAL"         # optional, already billions
RRP_SERIES_ID = "RRPONTSYD"            # already billions, daily

WEEKLY_LOOKBACK = 8     # weekly obs per weekly series - covers a 4-week trend plus buffer for gaps
RRP_LOOKBACK_DAYS = 45  # daily obs for RRP - comfortably spans 8 weekly Wednesdays
MAX_ALIGN_GAP_DAYS = 5  # how far back an RRP daily print may lag a given Wednesday and still count
MIN_ALIGNED_POINTS = 5  # need "latest" + "1wk ago" + "4wk ago" at minimum


def _fetch_series(series_id: str, api_key: str, limit: int):
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
    # FRED marks holidays/missing prints with "."
    return [o for o in obs if o.get("value") not in (None, ".")]


def _nearest_on_or_before(values_by_date: dict, target_date: str, max_gap_days: int):
    target = datetime.strptime(target_date, "%Y-%m-%d")
    best = None
    for date_str, value in values_by_date.items():
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if d <= target and (target - d).days <= max_gap_days:
            if best is None or d > best[0]:
                best = (d, value)
    return best[1] if best else None


def collect() -> Envelope:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return Envelope(
            module="fed_liquidity",
            source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes="FRED_API_KEY not set - see src/collectors/rates.py for how to get one.",
        )

    weekly = {}  # label -> {date: value_bn}
    try:
        for label, (series_id, divisor) in REQUIRED_SERIES.items():
            obs = _fetch_series(series_id, api_key, WEEKLY_LOOKBACK)
            if len(obs) < MIN_ALIGNED_POINTS:
                return Envelope(
                    module="fed_liquidity", source="FRED",
                    retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
                    payload={},
                    notes=f"only {len(obs)} weekly observations for {series_id}, need >= {MIN_ALIGNED_POINTS} for a 4-week trend",
                )
            weekly[label] = {o["date"]: float(o["value"]) / divisor for o in obs}

        rrp_obs = _fetch_series(RRP_SERIES_ID, api_key, RRP_LOOKBACK_DAYS)
        if not rrp_obs:
            return Envelope(
                module="fed_liquidity", source="FRED",
                retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
                payload={}, notes=f"no observations returned for {RRP_SERIES_ID}",
            )
        rrp_by_date = {o["date"]: float(o["value"]) for o in rrp_obs}
    except Exception as exc:
        return Envelope(
            module="fed_liquidity", source="FRED",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"FRED request failed: {exc}",
        )

    # Bank reserves - best effort, "when available" per spec. A failure here
    # degrades the reading rather than failing the whole envelope.
    reserves_by_date = {}
    reserves_note = ""
    try:
        reserves_obs = _fetch_series(RESERVES_SERIES_ID, api_key, WEEKLY_LOOKBACK)
        reserves_by_date = {o["date"]: float(o["value"]) for o in reserves_obs}
        if len(reserves_by_date) < MIN_ALIGNED_POINTS:
            reserves_note = f"bank reserves ({RESERVES_SERIES_ID}) had too few observations this run - reserves fields omitted"
            reserves_by_date = {}
    except Exception as exc:
        reserves_note = f"bank reserves ({RESERVES_SERIES_ID}) fetch failed - reserves fields omitted: {exc}"

    # WALCL/WTREGEN are both "Wednesday level" series and should share dates;
    # anchor on WALCL's date list and require TGA + a usable RRP value for
    # each date. Skip (don't guess) any date we can't fully reconstruct.
    walcl_dates = sorted(weekly["fed_assets"].keys(), reverse=True)
    weekly_points = []  # newest first
    for date in walcl_dates:
        fed_assets = weekly["fed_assets"].get(date)
        tga = weekly["tga"].get(date)
        rrp = rrp_by_date.get(date)
        if rrp is None:
            rrp = _nearest_on_or_before(rrp_by_date, date, MAX_ALIGN_GAP_DAYS)
        if fed_assets is None or tga is None or rrp is None:
            continue
        reserves = reserves_by_date.get(date)
        weekly_points.append({
            "date": date,
            "fed_assets_bn": round(fed_assets, 1),
            "tga_bn": round(tga, 1),
            "rrp_bn": round(rrp, 1),
            "reserves_bn": round(reserves, 1) if reserves is not None else None,
            "net_liquidity_bn": round(fed_assets - tga - rrp, 1),
        })

    if len(weekly_points) < MIN_ALIGNED_POINTS:
        return Envelope(
            module="fed_liquidity", source="FRED",
            retrieved_at=now_utc_iso(),
            observation_date=weekly_points[0]["date"] if weekly_points else None,
            status="FAILED", payload={},
            notes=(
                f"only {len(weekly_points)} fully-aligned weekly points (need >= {MIN_ALIGNED_POINTS} "
                "for a 4-week trend) - WALCL/WTREGEN/RRP series dates may have diverged"
            ),
        )

    latest, prior, four_wk_ago = weekly_points[0], weekly_points[1], weekly_points[4]

    def _chg(key, a, b):
        av, bv = a.get(key), b.get(key)
        return round(av - bv, 1) if av is not None and bv is not None else None

    payload = {
        "as_of": latest["date"],
        "fed_assets_bn": latest["fed_assets_bn"],
        "tga_bn": latest["tga_bn"],
        "rrp_bn": latest["rrp_bn"],
        "reserves_bn": latest["reserves_bn"],
        "net_liquidity_bn": latest["net_liquidity_bn"],
        "fed_assets_change_1w_bn": _chg("fed_assets_bn", latest, prior),
        "tga_change_1w_bn": _chg("tga_bn", latest, prior),
        "rrp_change_1w_bn": _chg("rrp_bn", latest, prior),
        "reserves_change_1w_bn": _chg("reserves_bn", latest, prior),
        "net_liquidity_change_1w_bn": _chg("net_liquidity_bn", latest, prior),
        "fed_assets_change_4w_bn": _chg("fed_assets_bn", latest, four_wk_ago),
        "tga_change_4w_bn": _chg("tga_bn", latest, four_wk_ago),
        "reserves_change_4w_bn": _chg("reserves_bn", latest, four_wk_ago),
        "net_liquidity_change_4w_bn": _chg("net_liquidity_bn", latest, four_wk_ago),
        # oldest -> newest, most recent 5 weekly points, for the trend list.
        "trend_5w": list(reversed(weekly_points[:5])),
    }

    return Envelope(
        module="fed_liquidity",
        source="FRED (WALCL, WTREGEN, RRPONTSYD" + (", WRESBAL" if payload["reserves_bn"] is not None else "") + ")",
        retrieved_at=now_utc_iso(),
        observation_date=latest["date"],
        status="EOD",
        payload=payload,
        notes=reserves_note,
    )


if __name__ == "__main__":
    env = collect()
    print(f"[{env.status}] {env.module} (obs {env.observation_date})")
    print(env.notes)
    for k, v in env.payload.items():
        print(f"  {k}: {v}")
