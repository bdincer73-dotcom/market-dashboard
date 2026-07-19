"""
Weekly Bear Indicator - ported from the user's Options P&L Tracker
("Bear Indicator" tab). Combines:

  - Chaikin Money Flow (20-period) for SPY, VOO, IWV, QQQ, RSP
  - Breadth (% above 50D/200D SMA) and Net New Hi % - both come from the
    breadth collector's payload, passed in rather than re-downloaded
  - SPY and RSP's price position vs their own 20D/50D SMA
  - A composite score (0-8) counting how many bearish conditions are true,
    and a Signal bucket derived from that score

This intentionally reproduces the tracker's ORIGINAL composite formula (the
one used for the 6/12-7/02 rows), not the ad-hoc replacement formula that
appeared in the 7/11 row - that later formula used a different scale but
kept the same 2/4/6 bucket thresholds, which don't line up. Flagged to the
user; can be changed in scoring() below if they'd rather keep the newer one.

Cadence: weekly only (matches the tracker, which is updated ~weekly, not
daily). main.py only calls this on `--run-type weekly` runs.
"""

from src.envelope import Envelope, now_utc_iso

CMF_PERIOD = 20
CMF_TICKERS = ["SPY", "VOO", "IWV", "QQQ", "RSP"]


def _chaikin_money_flow(high, low, close, volume, period=CMF_PERIOD):
    mf_multiplier = ((close - low) - (high - close)) / (high - low).replace(0, float("nan"))
    mf_volume = mf_multiplier * volume
    cmf = mf_volume.rolling(period).sum() / volume.rolling(period).sum()
    return cmf.iloc[-1]


def _ma_position(close_series, label: str) -> str:
    last = close_series.iloc[-1]
    sma20 = close_series.rolling(20).mean().iloc[-1]
    sma50 = close_series.rolling(50).mean().iloc[-1]
    above20, above50 = last > sma20, last > sma50
    if above20 and above50:
        return "Above both"
    if not above20 and not above50:
        return "Below both"
    return "Mixed (above 50, below 20)" if above50 else "Mixed (above 20, below 50)"


def collect(breadth_payload: dict) -> Envelope:
    import yfinance as yf

    try:
        raw = yf.download(
            tickers=CMF_TICKERS,
            period="4mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        return Envelope(
            module="bear_indicator",
            source="yfinance + breadth module",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"CMF download failed: {exc}",
        )

    cmf = {}
    observation_date = None
    missing = []
    for ticker in CMF_TICKERS:
        try:
            df = raw[ticker][["High", "Low", "Close", "Volume"]].dropna()
        except (KeyError, TypeError):
            missing.append(ticker)
            continue
        if len(df) < CMF_PERIOD + 1:
            missing.append(ticker)
            continue
        cmf[ticker] = round(
            float(_chaikin_money_flow(df["High"], df["Low"], df["Close"], df["Volume"])), 4
        )
        candidate_date = df.index[-1]
        if observation_date is None or candidate_date > observation_date:
            observation_date = candidate_date

    if "SPY" not in cmf or "RSP" not in cmf:
        return Envelope(
            module="bear_indicator",
            source="yfinance + breadth module",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"missing benchmark CMF (SPY/RSP required): {missing}",
        )

    spy_close = raw["SPY"]["Close"].dropna()
    rsp_close = raw["RSP"]["Close"].dropna()
    spy_vs_ma = _ma_position(spy_close, "SPY")
    rsp_vs_ma = _ma_position(rsp_close, "RSP")

    payload = {
        "cmf": cmf,
        "spy_vs_ma": spy_vs_ma,
        "rsp_vs_ma": rsp_vs_ma,
        "pct_above_50": breadth_payload.get("pct_above_50"),
        "pct_above_200": breadth_payload.get("pct_above_200"),
        "net_new_high_pct": breadth_payload.get("net_new_high_pct"),
        "missing_cmf_tickers": missing,
    }

    return Envelope(
        module="bear_indicator",
        source="yfinance + breadth module",
        retrieved_at=now_utc_iso(),
        observation_date=str(observation_date.date()) if observation_date is not None else None,
        status="EOD",
        payload=payload,
        notes="" if not missing else f"missing (non-fatal): {missing}",
    )


def score(payload: dict, prior_payload: dict | None) -> tuple[int, str, list[str]]:
    """Ports the tracker's 8-condition composite formula (rows 6/12-8/02).
    Returns (score 0-8, signal bucket, human-readable reasons)."""
    reasons = []
    cmf = payload["cmf"]
    core_avg = sum(cmf[t] for t in ["SPY", "VOO", "IWV", "QQQ"]) / 4
    all_min = min(cmf[t] for t in CMF_TICKERS)
    pct50, pct200 = payload["pct_above_50"], payload["pct_above_200"]
    net_new_hi = payload["net_new_high_pct"]

    s = 0
    if core_avg < 0.05:  # matches the tracker's AVERAGE(B:E)<0.05 (raw CMF units)
        s += 1; reasons.append(f"Avg core CMF weak ({core_avg:.3f})")
    if all_min < 0:
        s += 1; reasons.append(f"At least one index CMF negative (min {all_min:.2f})")
    if pct50 is not None and pct50 < 50.0:
        s += 1; reasons.append(f"%>50DMA below 50% ({pct50}%)")
    if pct200 is not None and pct200 < 55.0:
        s += 1; reasons.append(f"%>200DMA below 55% ({pct200}%)")
    if net_new_hi is not None and net_new_hi < 2.0:
        s += 1; reasons.append(f"Net New Hi% below 2% ({net_new_hi}%)")
    if "below both" in payload["spy_vs_ma"].lower():
        s += 1; reasons.append("SPY below both 20D and 50D SMA")

    if prior_payload:
        prior_cmf = prior_payload.get("cmf", {})
        prior_core_avg = (
            sum(prior_cmf[t] for t in ["SPY", "VOO", "IWV", "QQQ"]) / 4
            if all(t in prior_cmf for t in ["SPY", "VOO", "IWV", "QQQ"]) else None
        )
        if prior_core_avg is not None and core_avg < prior_core_avg * 0.6:
            s += 1; reasons.append(f"Flows decelerated sharply vs prior week ({core_avg:.2f} < 60% of {prior_core_avg:.2f})")
        prior_pct50 = prior_payload.get("pct_above_50")
        if prior_pct50 is not None and pct50 is not None and pct50 < prior_pct50 - 5.0:
            s += 1; reasons.append(f"%>50DMA dropped >5pts vs prior week ({prior_pct50}% -> {pct50}%)")
    else:
        reasons.append("No prior week snapshot yet - week-over-week checks skipped")

    if s >= 6:
        signal = "Bearish"
    elif s >= 4:
        signal = "Cautionary"
    elif s >= 2:
        signal = "Watch"
    else:
        signal = "Constructive"

    return s, signal, reasons


SIGNAL_ICON = {
    "Bearish": "\U0001F534",       # red circle
    "Cautionary": "\U0001F7E0",    # orange circle
    "Watch": "\U0001F7E1",         # yellow circle
    "Constructive": "\U0001F7E2",  # green circle
}
