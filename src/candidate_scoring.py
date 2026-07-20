"""
CSP candidate scoring - the blueprint's R2 "return/liquidity/event filters."

For each watchlist ticker, picks ONE representative put (not every strike -
that would be dozens of rows per ticker) out of the puts options_chain.py
already narrowed to the 30-40 DTE / OTM band:

  1. Apply the three gates: return >= min_return_target_pct, liquidity
     (spread% and open interest), and no earnings report before expiry.
  2. Among strikes that pass ALL gates, keep the one closest to the target
     delta band (a reasonable middle-of-the-road CSP strike) - not just the
     single highest return, since the highest-return strike is usually the
     most aggressive (deepest ITM-leaning) one and not what you'd actually
     want to default to.
  3. If nothing passes all gates, still surface the single best near-miss
     candidate with the specific reason(s) it failed, per the blueprint:
     "Exclude from action list; retain in watch section."

This is intentionally one row per ticker, not an exhaustive options scanner.
"""

CALC_VERSION = "r2-candidates-v1"


def _annualized_return_pct(mid: float, strike: float, dte: int) -> float | None:
    if not mid or not strike or not dte:
        return None
    return round((mid / strike) * (365.0 / dte) * 100, 2)


def _earnings_before_expiry(earnings_event: dict | None, dte: int) -> bool:
    if not earnings_event:
        return False
    days = earnings_event.get("days_to_event")
    return days is not None and 0 <= days <= dte


def _evaluate(put: dict, earnings_event: dict | None, thresholds: dict) -> dict:
    w = thresholds["watchlist"]
    ann_return = _annualized_return_pct(put["mid"], put["strike"], put["dte"])
    reasons_failed = []

    if ann_return is None or ann_return < w["min_return_target_pct"]:
        reasons_failed.append(
            f"return {ann_return}% < {w['min_return_target_pct']}% target" if ann_return is not None
            else "no usable mid price to compute return"
        )
    if put["spread_pct"] is None or put["spread_pct"] > w["max_spread_pct_of_mid"]:
        reasons_failed.append(
            f"bid/ask spread {put['spread_pct']}% > {w['max_spread_pct_of_mid']}% of mid"
            if put["spread_pct"] is not None else "no bid/ask to compute spread"
        )
    if put["open_interest"] < w["min_open_interest"]:
        reasons_failed.append(f"open interest {put['open_interest']} < {w['min_open_interest']} minimum")
    if _earnings_before_expiry(earnings_event, put["dte"]):
        reasons_failed.append(
            f"earnings on {earnings_event['date']} falls before this expiry ({put['dte']}D out)"
        )
    if put["delta_est"] is None:
        reasons_failed.append("delta could not be estimated (missing/zero implied vol)")

    return {
        **put,
        "annualized_return_pct": ann_return,
        "passes_all_gates": not reasons_failed,
        "fail_reasons": reasons_failed,
    }


def score_ticker(ticker: str, puts: list, earnings_event: dict | None, thresholds: dict) -> dict | None:
    if not puts:
        return None

    w = thresholds["watchlist"]
    target_mid = (w["target_delta_low"] + w["target_delta_high"]) / 2

    evaluated = [_evaluate(p, earnings_event, thresholds) for p in puts]
    passing = [e for e in evaluated if e["passes_all_gates"]]

    if passing:
        # Closest to the target delta band, not just highest return.
        best = min(
            passing,
            key=lambda e: abs((e["delta_est"] or -999) - target_mid),
        )
        return {"ticker": ticker, "status": "CANDIDATE", "pick": best, "n_evaluated": len(evaluated)}

    # Nothing passed - surface the closest near-miss (highest annualized
    # return among evaluated) so it's visible in the watch section, not lost.
    with_return = [e for e in evaluated if e["annualized_return_pct"] is not None]
    if not with_return:
        return {"ticker": ticker, "status": "NO_USABLE_DATA", "pick": None, "n_evaluated": len(evaluated)}
    near_miss = max(with_return, key=lambda e: e["annualized_return_pct"])
    return {"ticker": ticker, "status": "WATCH_ONLY", "pick": near_miss, "n_evaluated": len(evaluated)}


def score_all(options_payload: dict, earnings_payload: dict, thresholds: dict) -> dict:
    results = []
    for ticker, data in options_payload.get("tickers", {}).items():
        if data.get("status") != "OK":
            results.append({"ticker": ticker, "status": "NO_CHAIN_DATA", "pick": None, "n_evaluated": 0})
            continue
        earnings_entry = earnings_payload.get("tickers", {}).get(ticker, {})
        earnings_event = earnings_entry.get("event") if earnings_entry.get("status") == "SCHEDULED" else None
        result = score_ticker(ticker, data.get("puts", []), earnings_event, thresholds)
        if result:
            result["spot"] = data.get("spot")
            results.append(result)

    candidates = [r for r in results if r["status"] == "CANDIDATE"]
    watch_only = [r for r in results if r["status"] == "WATCH_ONLY"]
    no_data = [r for r in results if r["status"] in ("NO_CHAIN_DATA", "NO_USABLE_DATA")]

    candidates.sort(key=lambda r: r["pick"]["annualized_return_pct"], reverse=True)
    watch_only.sort(key=lambda r: r["pick"]["annualized_return_pct"] if r["pick"] else -999, reverse=True)

    return {
        "candidates": candidates,
        "watch_only": watch_only,
        "no_data": no_data,
        "candidate_count": len(candidates),
        "watch_only_count": len(watch_only),
    }
