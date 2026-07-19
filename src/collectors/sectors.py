"""
Sector rotation collector: SPY, RSP, and the 11 Select Sector SPDR ETFs.

Rank change vs the prior session is computed in src/store.py (needs history),
not here - this module only produces the current snapshot.
"""

from src.envelope import Envelope, now_utc_iso

SECTOR_ETFS = {
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLK": "Technology",
    "XLU": "Utilities",
}
BENCHMARKS = {"SPY": "S&P 500 (cap-weighted)", "RSP": "S&P 500 (equal-weighted)"}
DEFENSIVE_SECTORS = {"XLP", "XLU", "XLV", "XLRE"}

ALL_TICKERS = list(SECTOR_ETFS) + list(BENCHMARKS)


def _total_return(closes, days: int):
    if len(closes) <= days:
        return None
    return round(100 * (closes.iloc[-1] / closes.iloc[-1 - days] - 1), 2)


def collect() -> Envelope:
    import yfinance as yf

    try:
        raw = yf.download(
            tickers=ALL_TICKERS,
            period="9mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        return Envelope(
            module="sectors",
            source="yfinance",
            retrieved_at=now_utc_iso(),
            observation_date=None,
            status="FAILED",
            payload={},
            notes=f"download failed: {exc}",
        )

    rows = {}
    observation_date = None
    missing = []

    for ticker in ALL_TICKERS:
        try:
            closes = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            missing.append(ticker)
            continue
        if closes.empty:
            missing.append(ticker)
            continue
        rows[ticker] = {
            "return_1d": _total_return(closes, 1),
            "return_5d": _total_return(closes, 5),
            "return_20d": _total_return(closes, 20),
            "return_3m": _total_return(closes, 63),
        }
        candidate_date = closes.index[-1]
        if observation_date is None or candidate_date > observation_date:
            observation_date = candidate_date

    if missing:
        # A missing benchmark (SPY/RSP) is fatal for relative-return math.
        if "SPY" in missing or "RSP" in missing:
            return Envelope(
                module="sectors",
                source="yfinance",
                retrieved_at=now_utc_iso(),
                observation_date=None,
                status="FAILED",
                payload={},
                notes=f"missing benchmark ticker(s): {missing}",
            )

    spy_5d = rows.get("SPY", {}).get("return_5d")
    rsp_5d = rows.get("RSP", {}).get("return_5d")
    rsp_vs_spy_5d = round(rsp_5d - spy_5d, 2) if spy_5d is not None and rsp_5d is not None else None

    sector_rows = []
    for ticker, name in SECTOR_ETFS.items():
        if ticker not in rows:
            continue
        r = rows[ticker]
        rel_vs_spy = (
            round(r["return_20d"] - rows["SPY"]["return_20d"], 2)
            if r["return_20d"] is not None and rows.get("SPY", {}).get("return_20d") is not None
            else None
        )
        sector_rows.append({
            "ticker": ticker,
            "name": name,
            "is_defensive": ticker in DEFENSIVE_SECTORS,
            "return_1d": r["return_1d"],
            "return_5d": r["return_5d"],
            "return_20d": r["return_20d"],
            "return_3m": r["return_3m"],
            "relative_return_20d_vs_spy": rel_vs_spy,
        })

    # Rank by 20D relative strength, best first. Rank change vs yesterday is
    # filled in later once we have history in the store.
    ranked = sorted(
        [s for s in sector_rows if s["relative_return_20d_vs_spy"] is not None],
        key=lambda s: s["relative_return_20d_vs_spy"],
        reverse=True,
    )
    for i, s in enumerate(ranked, start=1):
        s["rank"] = i

    payload = {
        "benchmarks": rows.get("SPY", {}) | {"ticker": "SPY"} if "SPY" in rows else {},
        "rsp": rows.get("RSP", {}) | {"ticker": "RSP"} if "RSP" in rows else {},
        "rsp_vs_spy_5d": rsp_vs_spy_5d,
        "sectors": ranked if ranked else sector_rows,
        "missing_tickers": missing,
    }

    return Envelope(
        module="sectors",
        source="yfinance",
        retrieved_at=now_utc_iso(),
        observation_date=str(observation_date.date()) if observation_date is not None else None,
        status="EOD",
        payload=payload,
        notes="" if not missing else f"missing (non-fatal): {missing}",
    )
