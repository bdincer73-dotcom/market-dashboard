"""
Volatility collector: VIX, VIX9D, VIX3M, VVIX via yfinance (free, delayed EOD
for index tickers). Option-chain IV/OI/Greeks for the watchlist are an R2 item
(needs a broker or licensed vendor) - not collected here.
"""

from src.envelope import Envelope, now_utc_iso

TICKERS = {
    "VIX": "^VIX",
    "VIX9D": "^VIX9D",
    "VIX3M": "^VIX3M",
    "VVIX": "^VVIX",
}


def _pct_change(closes, days: int):
    if len(closes) <= days:
        return None
    return round(closes.iloc[-1] - closes.iloc[-1 - days], 2)


def collect() -> Envelope:
    import yfinance as yf

    try:
        raw = yf.download(
            tickers=list(TICKERS.values()),
            period="3mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        return Envelope(
            module="volatility",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"download failed: {exc}",
        )

    values = {}
    observation_date = None
    missing = []
    for label, ticker in TICKERS.items():
        try:
            closes = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            missing.append(label)
            continue
        if closes.empty:
            missing.append(label)
            continue
        values[label] = {
            "level": round(closes.iloc[-1], 2),
            "change_1d": _pct_change(closes, 1),
            "change_5d": _pct_change(closes, 5),
        }
        candidate_date = closes.index[-1]
        if observation_date is None or candidate_date > observation_date:
            observation_date = candidate_date

    if "VIX" not in values:
        return Envelope(
            module="volatility",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"VIX itself unavailable; missing={missing}",
        )

    vix3m_minus_vix = (
        round(values["VIX3M"]["level"] - values["VIX"]["level"], 2)
        if "VIX3M" in values else None
    )
    curve_state = None
    if vix3m_minus_vix is not None:
        curve_state = "contango (calm)" if vix3m_minus_vix > 0 else "backwardation (stress)"

    payload = {
        **values,
        "vix3m_minus_vix": vix3m_minus_vix,
        "curve_state": curve_state,
        "missing": missing,
    }

    return Envelope(
        module="volatility",
        source="yfinance",
        retrieved_at=now_utc_iso(),
        observation_date=str(observation_date.date()) if observation_date is not None else None,
        status="EOD",
        payload=payload,
        notes="" if not missing else f"missing (non-fatal): {missing}",
    )
