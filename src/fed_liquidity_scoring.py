"""
Fed Liquidity Indicator - scoring.

Turns the raw weekly payload from src/collectors/fed_liquidity.py into a
0-100 Liquidity Score, an Expanding / Neutral / Contracting regime, and the
two narrative fields this overlay exists for: impact on high-beta/AI-
adjacent names, and a direct CSP position-sizing/risk implication. Kept
separate from the collector per this repo's source -> calculate -> score
convention (same split as fomo_fragility.py / fomo_scoring.py).

Score construction: start at 50 (neutral) and apply three signed, clamped
adjustments - this week's $ move in Net Liquidity, the 4-week Net Liquidity
trend, and bank reserves' own 4-week move (reserves get their own term
because they're arguably the "cleanest" Fed-driven read - Net Liquidity
mixes in TGA/RRP swings that are Treasury- and money-market-driven, not
Fed-driven; see the collector docstring). Each term is ramped against a
configurable scale so no single week's noise can swing the score to an
extreme by itself. This is a simple, tunable heuristic, not a formal Fed
liquidity model - every scale/weight lives in config/thresholds.yaml under
`fed_liquidity`, edit that file (not this one) to retune sensitivity.
"""

from __future__ import annotations

CALC_VERSION = "fed-liquidity-v1"

DEFAULT_THRESHOLDS = {
    "weekly_change_scale_bn": 200.0,
    "weekly_weight": 15.0,
    "four_week_change_scale_bn": 500.0,
    "four_week_weight": 20.0,
    "reserves_four_week_scale_bn": 450.0,
    "reserves_weight": 15.0,
    "expanding_score_min": 60.0,
    "contracting_score_max": 40.0,
    "severe_score_max": 20.0,
}

_IMPACT_HIGH_BETA = {
    "Expanding": (
        "Tailwind for high-beta/AI-adjacent names - expanding liquidity has historically "
        "coincided with multiple expansion and risk-on flows into the most speculative, "
        "high-multiple corners of the market."
    ),
    "Neutral": (
        "Limited macro overlay effect either way - lean on breadth/technicals for high-beta "
        "positioning rather than this indicator."
    ),
    "Contracting": (
        "Headwind for high-beta/AI-adjacent names - contracting liquidity has historically "
        "coincided with multiple compression concentrated in the most rate- and "
        "duration-sensitive, high-multiple growth names, even when breadth still looks fine."
    ),
}

_CSP_IMPLICATION = {
    "Expanding": (
        "Liquidity tailwind supports normal CSP sizing, but this overlay alone should never be "
        "a reason to size UP - confirm with breadth/technicals and the regime call first."
    ),
    "Neutral": (
        "No adjustment from this overlay - size and strike selection should be driven by the "
        "regime call and CSP candidate gates as usual."
    ),
    "Contracting": (
        "Reduce CSP aggressiveness: favor lower-beta / higher-quality underlyings, wider "
        "(lower-delta) strikes, and smaller size - even if breadth/technicals still look fine. "
        "Contracting liquidity is a reason to lean more conservative than the regime call alone "
        "would suggest."
    ),
}


def _signed_ramp(x: float, scale: float) -> float:
    """Linear in [-scale, scale] -> [-1, 1], clamped outside that range."""
    if not scale:
        return 0.0
    return max(-1.0, min(1.0, x / scale))


def _fmt_signed(x: float | None, unit: str = "B") -> str:
    if x is None:
        return "n/a"
    return f"{'+' if x >= 0 else ''}{x:.0f}{unit}"


def score(payload: dict, thresholds: dict | None = None):
    """Returns (liquidity_score 0-100, regime, reasons, calc_payload) -
    matches the (score, signal, reasons, calc_payload) shape the other
    weekly modules (bear_indicator, fomo_scoring) return, so main.py can
    treat all three uniformly."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    weekly_change = payload["net_liquidity_change_1w_bn"]
    four_week_change = payload["net_liquidity_change_4w_bn"]
    reserves_4w = payload.get("reserves_change_4w_bn")

    weekly_component = _signed_ramp(weekly_change, t["weekly_change_scale_bn"]) * t["weekly_weight"]
    trend_component = _signed_ramp(four_week_change, t["four_week_change_scale_bn"]) * t["four_week_weight"]
    reserves_component = (
        _signed_ramp(reserves_4w, t["reserves_four_week_scale_bn"]) * t["reserves_weight"]
        if reserves_4w is not None else 0.0
    )

    liquidity_score = round(
        max(0.0, min(100.0, 50.0 + weekly_component + trend_component + reserves_component)), 1
    )

    if liquidity_score >= t["expanding_score_min"]:
        regime = "Expanding"
        icon = "\U0001F7E2"  # green circle
    elif liquidity_score <= t["contracting_score_max"]:
        regime = "Contracting"
        icon = "\U0001F534" if liquidity_score <= t["severe_score_max"] else "\U0001F7E0"  # red / orange
    else:
        regime = "Neutral"
        icon = "⚪"  # white circle

    signal = f"{icon} FED LIQUIDITY: {regime.upper()}"

    reasons = [
        f"Net Liquidity {_fmt_signed(weekly_change)} this week "
        f"(Fed assets {_fmt_signed(payload.get('fed_assets_change_1w_bn'))}, "
        f"TGA {_fmt_signed(payload.get('tga_change_1w_bn'))}, "
        f"ON RRP {_fmt_signed(payload.get('rrp_change_1w_bn'))})",
        f"4-week Net Liquidity trend: {_fmt_signed(four_week_change)}",
    ]
    if reserves_4w is not None:
        reasons.append(
            f"Bank reserves: {_fmt_signed(payload.get('reserves_change_1w_bn'))} this week, "
            f"{_fmt_signed(reserves_4w)} over 4 weeks"
        )
    else:
        reasons.append("Bank reserves (WRESBAL) unavailable this run - reserves term excluded from the score")

    # A TGA-driven drain (Treasury absorbing cash, not the Fed doing QT) can
    # reverse fast once Treasury starts spending it back out - flag that
    # distinction explicitly since it changes how much weight a Contracting
    # print deserves versus one driven by the Fed's own balance sheet.
    tga_4w = payload.get("tga_change_4w_bn")
    fed_assets_4w = payload.get("fed_assets_change_4w_bn")
    if regime == "Contracting" and tga_4w is not None and fed_assets_4w is not None \
            and tga_4w > 0 and fed_assets_4w >= 0:
        reasons.append(
            f"Drain looks TGA-driven, not Fed QT: TGA {_fmt_signed(tga_4w)} over 4 weeks vs "
            f"Fed assets {_fmt_signed(fed_assets_4w)} - can reverse quickly once Treasury "
            "spends the cash back out."
        )

    calc_payload = {
        "score": liquidity_score,
        "regime": regime,
        "impact_high_beta": _IMPACT_HIGH_BETA[regime],
        "csp_implication": _CSP_IMPLICATION[regime],
    }
    return liquidity_score, regime, reasons, calc_payload
