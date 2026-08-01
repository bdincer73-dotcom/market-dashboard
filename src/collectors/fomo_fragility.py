"""
FOMO Fragility Index collector — Nasdaq euphoria/top module.

Companion to the weekly Bear Indicator, but for the opposite failure mode:
the Bear Indicator catches a downtrend (flows/breadth rolling over); this
catches a levered, crowded, over-extended TOP where one shock forces a
liquidation cascade. Runs weekly, same cadence as the Bear Indicator.

Gathers the raw inputs for three axes - Stretch, Leverage, Crowding - all
from free sources, no paid feeds, matching the rest of this project:

  - Stretch (price over-extension): NDX constituent list + weights from
    slickcharts.com (unofficial but free/reliable HTML table - Wikipedia
    doesn't maintain a full Nasdaq-100 constituent table the way it does
    for the S&P 500, so this fills that gap), OHLCV from yfinance.
  - Leverage (the fuel): FINRA's public margin-statistics page (official,
    government-mandated disclosure, ~4-week lag - inherently monthly) and
    VXN (yfinance).
  - Crowding (positioning concentration): top-10 NDX weight (from the same
    slickcharts table) and average pairwise correlation of an AI-hardware
    basket (yfinance).

Scoring (axis math, composite, bands) lives in src/fomo_scoring.py, kept
separate per this repo's source -> calculate -> score pipeline convention.
This module only gathers and validates raw numbers.
"""

import io
import numpy as np
import pandas as pd
import requests

from src.envelope import Envelope, now_utc_iso

SLICKCHARTS_NDX_URL = "https://www.slickcharts.com/nasdaq100"
FINRA_MARGIN_URL = "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"

MIN_NDX_COVERAGE_PCT = 90.0  # slightly looser than breadth.py's 95% - NDX list is smaller (~100) so one bad ticker moves the needle more
CONSTITUENT_HISTORY_PERIOD = "500d"  # matches breadth.py - enough trading days for a trailing 200D SMA
INDEX_HISTORY_PERIOD = "6mo"         # ^NDX/^VXN only need RSI14 (14d) + 20D high - 6mo is plenty

# AI-hardware basket for the crowding axis's correlation component. Fixed
# list (not the user's watchlist) since this measures a market-wide
# phenomenon, not portfolio-specific exposure.
CORRELATION_BASKET = ["MU", "WDC", "STX", "COHR", "GLW", "MRVL", "TER", "LRCX", "CLS", "ASML"]
CORRELATION_WINDOW_DAYS = 60

TOP_N_CONCENTRATION = 10


def get_ndx_constituents_and_weights() -> pd.DataFrame:
    """Symbol + Weight (%) for every current Nasdaq-100 member, from
    slickcharts.com. This is an unofficial aggregator (not the exchange or
    Invesco directly - see README for why), but it's the only free source
    found that publishes both the full constituent list AND per-name
    weights in one table; Wikipedia's Nasdaq-100 page doesn't carry a full
    components table the way its S&P 500 page does."""
    resp = requests.get(
        SLICKCHARTS_NDX_URL,
        headers={"User-Agent": "Mozilla/5.0 (market-dashboard research bot)"},
        timeout=30,
    )
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0][["Symbol", "Weight"]].copy()
    df["Weight"] = df["Weight"].astype(str).str.rstrip("%").astype(float)
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    return df


def get_finra_margin_stats() -> pd.DataFrame:
    """Latest ~13 months of FINRA margin debt / free credit balances,
    scraped from the server-rendered HTML table on FINRA's own margin
    statistics page (not the versioned static XLSX link, which isn't
    guaranteed to be the latest release - the page's own table always is).
    Values in $ millions."""
    resp = requests.get(
        FINRA_MARGIN_URL,
        headers={"User-Agent": "Mozilla/5.0 (market-dashboard research bot)"},
        timeout=30,
    )
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    for t in tables:
        cols = list(t.columns)
        if any("Debit Balances" in str(c) for c in cols):
            t = t.rename(columns={
                "Month/Year": "month",
                [c for c in cols if "Debit Balances" in str(c)][0]: "debit_balances",
                [c for c in cols if "Cash Accounts" in str(c)][0]: "free_credit_cash",
                [c for c in cols if "Securities Margin Accounts" in str(c) and "Free Credit" in str(c)][0]: "free_credit_margin",
            })
            return t
    raise ValueError("could not find the margin-statistics table on FINRA's page (page structure may have changed)")


def _rsi14(closes: pd.Series) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _mean_pairwise_correlation(returns: pd.DataFrame) -> float:
    corr = returns.corr().values
    n = corr.shape[0]
    if n < 2:
        return 0.0
    return float((corr.sum() - n) / (n * n - n))


def collect(_ndx_tickers_override: list[str] | None = None) -> Envelope:
    """`_ndx_tickers_override` is for tests only, to check the SMA/RSI math
    against a small ticker subset without downloading all ~100 NDX names."""
    import yfinance as yf

    notes_parts = []

    # --- NDX constituents + weights (stretch axis universe + crowding's top-10 weight) ---
    try:
        ndx_df = get_ndx_constituents_and_weights()
        ndx_tickers = _ndx_tickers_override if _ndx_tickers_override is not None else ndx_df["Symbol"].tolist()
        top10_pct_of_ndx = round(ndx_df.sort_values("Weight", ascending=False).head(TOP_N_CONCENTRATION)["Weight"].sum() / 100, 4)
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="slickcharts+finra+yfinance",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"could not fetch NDX constituent/weight list: {exc}",
        )

    # --- NDX constituent OHLCV: dispersion (stretch axis) ---
    try:
        raw = yf.download(
            tickers=ndx_tickers, period=CONSTITUENT_HISTORY_PERIOD, interval="1d",
            group_by="ticker", auto_adjust=True, threads=True, progress=False,
        )
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="yfinance",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"NDX constituent bulk download failed: {exc}",
        )

    over_2sigma = over_200dma = usable = 0
    observation_date = None
    for ticker in ndx_tickers:
        try:
            closes = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(closes) < 200:
            continue
        usable += 1
        last = closes.iloc[-1]
        sma50 = closes.rolling(50).mean().iloc[-1]
        std50 = closes.rolling(50).std().iloc[-1]
        sma200 = closes.rolling(200).mean().iloc[-1]
        if std50 and std50 > 0 and (last - sma50) / std50 > 2.0:
            over_2sigma += 1
        if last > sma200:
            over_200dma += 1
        candidate_date = closes.index[-1]
        if observation_date is None or candidate_date > observation_date:
            observation_date = candidate_date

    coverage_pct = round(100 * usable / len(ndx_tickers), 1) if ndx_tickers else 0.0
    if coverage_pct < MIN_NDX_COVERAGE_PCT:
        return Envelope(
            module="fomo_fragility", source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=str(observation_date.date()) if observation_date is not None else None,
            status="FAILED", payload={},
            notes=f"NDX symbol coverage {coverage_pct}% below the {MIN_NDX_COVERAGE_PCT}% quality gate.",
        )

    # --- ^NDX index itself: RSI14 + proximity to 20D high ---
    try:
        ndx_hist = yf.download("^NDX", period=INDEX_HISTORY_PERIOD, interval="1d", auto_adjust=True, progress=False)
        ndx_close = ndx_hist["Close"].dropna()
        if isinstance(ndx_close, pd.DataFrame):
            ndx_close = ndx_close.iloc[:, 0]
        ndx_rsi14 = round(_rsi14(ndx_close), 1)
        ndx_pct_of_20d_high = round(float(ndx_close.iloc[-1] / ndx_close.iloc[-20:].max() * 100), 2)
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="yfinance",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"^NDX index history fetch failed: {exc}",
        )

    # --- VXN ---
    try:
        vxn_hist = yf.download("^VXN", period="10d", interval="1d", auto_adjust=True, progress=False)
        vxn_close = vxn_hist["Close"].dropna()
        if isinstance(vxn_close, pd.DataFrame):
            vxn_close = vxn_close.iloc[:, 0]
        vxn_level = round(float(vxn_close.iloc[-1]), 2)
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="yfinance",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"^VXN fetch failed: {exc}",
        )

    # --- FINRA margin debt YoY + net credit balance (leverage axis) ---
    try:
        margin_df = get_finra_margin_stats()
        if len(margin_df) < 13:
            raise ValueError(f"only {len(margin_df)} months in FINRA table, need 13 for a YoY comparison")
        latest, year_ago = margin_df.iloc[0], margin_df.iloc[12]
        margin_debt_yoy = round((latest["debit_balances"] - year_ago["debit_balances"]) / year_ago["debit_balances"], 4)
        credit_balance_tn = round(
            (latest["free_credit_cash"] + latest["free_credit_margin"] - latest["debit_balances"]) / 1_000_000, 4
        )
        margin_data_asof = str(latest["month"])
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="finra.org",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"FINRA margin statistics fetch/parse failed: {exc}",
        )

    # --- AI-hardware basket correlation (crowding axis) ---
    try:
        basket_raw = yf.download(
            tickers=CORRELATION_BASKET, period="4mo", interval="1d",
            group_by="ticker", auto_adjust=True, threads=True, progress=False,
        )
        closes = {}
        for t in CORRELATION_BASKET:
            try:
                closes[t] = basket_raw[t]["Close"].dropna()
            except (KeyError, TypeError):
                continue
        basket_df = pd.DataFrame(closes).dropna()
        if len(closes) < len(CORRELATION_BASKET) * 0.7:
            notes_parts.append(
                f"basket correlation computed from only {len(closes)}/{len(CORRELATION_BASKET)} tickers (some failed to fetch)"
            )
        returns = basket_df.pct_change().dropna().tail(CORRELATION_WINDOW_DAYS)
        basket_correlation = round(_mean_pairwise_correlation(returns), 3)
    except Exception as exc:
        return Envelope(
            module="fomo_fragility", source="yfinance",
            retrieved_at=now_utc_iso(), observation_date=None, status="FAILED",
            payload={}, notes=f"AI-hardware basket correlation fetch failed: {exc}",
        )

    payload = {
        "ndx_constituent_count": len(ndx_tickers),
        "ndx_usable_count": usable,
        "ndx_coverage_pct": coverage_pct,
        "pct_ndx_over_2sigma": round(over_2sigma / usable, 4),
        "pct_ndx_over_200dma": round(over_200dma / usable, 4),
        "ndx_rsi14": ndx_rsi14,
        "ndx_pct_of_20d_high": ndx_pct_of_20d_high,
        "margin_debt_yoy": margin_debt_yoy,
        "credit_balance_tn": credit_balance_tn,
        "margin_data_asof": margin_data_asof,
        "vxn_level": vxn_level,
        "top10_pct_of_ndx": top10_pct_of_ndx,
        "basket_correlation": basket_correlation,
        "basket_tickers": CORRELATION_BASKET,
    }

    return Envelope(
        module="fomo_fragility",
        source="slickcharts (NDX weights, unofficial) + yfinance (OHLCV/VXN) + finra.org (margin, official)",
        retrieved_at=now_utc_iso(),
        observation_date=str(observation_date.date()) if observation_date is not None else None,
        status="EOD",
        payload=payload,
        notes=(
            f"margin/credit data as of {margin_data_asof} (FINRA publishes ~4 weeks after month-end - "
            "this lags the price-based axes). NDX weights from slickcharts.com, an unofficial "
            "aggregator, not the exchange or Invesco directly."
            + (" " + "; ".join(notes_parts) if notes_parts else "")
        ),
    )


if __name__ == "__main__":
    env = collect()
    print(f"[{env.status}] {env.module} (obs {env.observation_date})")
    print(env.notes)
    for k, v in env.payload.items():
        print(f"  {k}: {v}")
