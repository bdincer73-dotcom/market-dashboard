"""
Put option chain collector (yfinance, free - no broker API).

For each watchlist `positions` ticker, pulls put chains for expiries that
fall inside the DTE window from config/watchlist.yaml (default 30-40 days),
keeps OTM strikes only (a CSP seller doesn't sell ITM puts), and computes:

  - mid price = (bid+ask)/2
  - bid/ask spread as % of mid (the blueprint's liquidity gate checks this)
  - approximate delta via Black-Scholes, using yfinance's impliedVolatility

IMPORTANT CAVEAT: yfinance does not provide real Greeks or a guaranteed-fresh
quote - lastTradeDate can lag, and delta here is *our own estimate*, not a
broker-verified value. Every candidate on the dashboard is labeled "delta
(est.)" for this reason. The blueprint's original design assumed real broker
option-chain access; this is the free-tier substitute, not a replacement.
"""

import math
import os
from datetime import date, datetime


def _safe_float(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default

import yaml
from pathlib import Path

from src.envelope import Envelope, now_utc_iso

WATCHLIST_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist.yaml"
THRESHOLDS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "thresholds.yaml"

# OTM strike band for puts we bother pulling: strike must be below spot, but
# not so far OTM the premium is negligible. Keeps chain data volume sane.
MIN_STRIKE_PCT_OF_SPOT = 0.55
MAX_STRIKE_PCT_OF_SPOT = 0.98

# Flat risk-free-rate estimate for the Black-Scholes delta approximation.
# Not pulled from the rates collector to avoid a hard dependency between
# collectors; close enough for an estimate, not for real Greeks anyway.
ASSUMED_RISK_FREE_RATE = 0.042


def load_watchlist() -> dict:
    with open(WATCHLIST_PATH) as f:
        return yaml.safe_load(f)


def load_dte_window() -> tuple[int, int]:
    with open(THRESHOLDS_PATH) as f:
        t = yaml.safe_load(f)
    w = t.get("watchlist", {})
    return w.get("min_dte", 30), w.get("max_dte", 40)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _put_delta(spot: float, strike: float, dte_days: int, iv: float, r: float = ASSUMED_RISK_FREE_RATE):
    if not iv or iv <= 0 or spot <= 0 or strike <= 0 or dte_days <= 0:
        return None
    t = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return None
    return round(_norm_cdf(d1) - 1.0, 3)  # put delta is negative


def collect() -> Envelope:
    import yfinance as yf

    wl = load_watchlist()
    tickers = list(wl.get("positions", []))
    min_dte, max_dte = load_dte_window()
    today = date.today()

    results = {}
    errors = []
    total_candidates = 0

    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            spot = t.fast_info.get("lastPrice") if hasattr(t, "fast_info") else None
            if not spot:
                spot = t.info.get("regularMarketPrice")
            expiries = t.options
        except Exception as exc:
            results[ticker] = {"status": "UNAVAILABLE", "reason": str(exc), "puts": []}
            errors.append(ticker)
            continue

        if not spot or not expiries:
            results[ticker] = {"status": "UNAVAILABLE", "reason": "no spot price or no option chain", "puts": []}
            errors.append(ticker)
            continue

        in_window_expiries = []
        for exp_str in expiries:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if min_dte <= dte <= max_dte:
                in_window_expiries.append((exp_str, dte))

        puts_out = []
        for exp_str, dte in in_window_expiries:
            try:
                chain = t.option_chain(exp_str)
                puts = chain.puts
            except Exception:
                continue
            if puts is None or puts.empty:
                continue
            lo, hi = spot * MIN_STRIKE_PCT_OF_SPOT, spot * MAX_STRIKE_PCT_OF_SPOT
            otm = puts[(puts["strike"] >= lo) & (puts["strike"] <= hi)]
            for _, row in otm.iterrows():
                bid, ask = _safe_float(row["bid"]), _safe_float(row["ask"])
                mid = round((bid + ask) / 2, 2) if (bid or ask) else 0.0
                spread_pct = round(100 * (ask - bid) / mid, 1) if mid > 0 else None
                iv = _safe_float(row["impliedVolatility"], default=None)
                delta = _put_delta(spot, float(row["strike"]), dte, iv)
                puts_out.append({
                    "expiry": exp_str,
                    "dte": dte,
                    "strike": float(row["strike"]),
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_pct": spread_pct,
                    "open_interest": int(_safe_float(row["openInterest"], default=0)),
                    "implied_vol": round(iv, 3) if iv is not None else None,
                    "delta_est": delta,
                })
        results[ticker] = {"status": "OK", "spot": round(float(spot), 2), "puts": puts_out}
        total_candidates += len(puts_out)

    if not results or all(r["status"] != "OK" for r in results.values()):
        return Envelope(
            module="options_chain",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"no usable option chains retrieved; errors: {errors}",
        )

    payload = {
        "tickers": results,
        "dte_window": [min_dte, max_dte],
        "total_raw_candidates": total_candidates,
        "errors": errors,
    }

    return Envelope(
        module="options_chain",
        source="yfinance",
        retrieved_at=now_utc_iso(),
        observation_date=today.isoformat(),
        status="EOD",
        payload=payload,
        notes=(
            "Delta is an estimate (Black-Scholes from yfinance implied vol), "
            "not a broker Greek. Quotes may lag real-time." +
            (f" Errors on: {errors}" if errors else "")
        ),
    )
