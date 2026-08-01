"""
FOMO Fragility Index — scoring.

Takes the raw payload from src/collectors/fomo_fragility.py and turns it into
the three axis scores (Stretch, Leverage, Crowding), the 0-100 composite, and
a band/signal. Kept separate from the collector per this repo's
source -> calculate -> score convention (same split as scoring.py/breadth,
candidate_scoring.py/options_chain).

Design principle, carried over from the original bundle: this fires on the
CONJUNCTION of axes, not any one alone. "Expensive" is not a signal;
"expensive + levered + crowded" is. See the RED conjunction guard below.
"""

from __future__ import annotations

CALC_VERSION = "fomo-fragility-v1"

AXIS_WEIGHTS = {
    "stretch": 0.35,
    "leverage": 0.40,   # the fuel - heaviest weight
    "crowding": 0.25,
}

BAND_BUILDING = 40
BAND_ELEVATED = 60
BAND_REDUCE = 75

# Conjunction guard: RED requires composite >= BAND_REDUCE AND at least this
# many axes individually above AXIS_HOT. Prevents "expensive-but-stable" from
# triggering - a real top needs multiple axes hot at once.
AXIS_HOT = 70
MIN_HOT_AXES_FOR_RED = 2

# "Near" the 20D high, for the breadth-divergence check (index making highs
# while participation narrows underneath it).
NEAR_20D_HIGH_PCT = 97.0

_ACTIONS = {
    "green": "No euphoria. Normal operations.",
    "building": "Stop adding leverage. Let cash reserve drift to the top of your target band. "
                "Widen CSP strikes (lower delta).",
    "elevated": "Actively raise cash. Close the most collateral-heavy / lowest-cushion puts. "
                "Trim convex lottery-ticket positions. Do NOT average down.",
    "reduce": "Cut position size significantly. Take LEAPS gains that are working. Sit on cash. "
              "This marks the top of the FOMO trade - expect it to fire weeks EARLY and stay red; "
              "that's correct, not a false alarm.",
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp(value: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.0
    return _clamp01((value - lo) / (hi - lo))


def compute_breadth_divergence(payload: dict, prior_payload: dict | None) -> bool:
    """Index near its 20D high while the % of names above their 200DMA is
    FALLING week over week - a classic narrowing-under-the-surface top
    signal. Needs a prior week's reading; with none (first-ever run),
    defaults to False rather than guessing which way it's moved."""
    near_high = payload["ndx_pct_of_20d_high"] >= NEAR_20D_HIGH_PCT
    if not prior_payload:
        return False
    breadth_falling = payload["pct_ndx_over_200dma"] < prior_payload.get("pct_ndx_over_200dma", 1.0)
    return bool(near_high and breadth_falling)


def score_stretch(payload: dict, breadth_divergence: bool) -> float:
    """High when many names are extended, index overbought, RSI hot, breadth diverging."""
    s = (
        _clamp01(payload["pct_ndx_over_2sigma"] / 0.20) * 0.35   # 20% of names >2sigma = max
        + _ramp(payload["pct_ndx_over_200dma"], 0.50, 0.90) * 0.25
        + _ramp(payload["ndx_rsi14"], 50, 80) * 0.25
        + (1.0 if breadth_divergence else 0.0) * 0.15
    )
    return round(100 * s, 1)


def score_leverage(payload: dict) -> float:
    """High when margin is surging, dry powder is gone, and vol is SUPPRESSED
    (low vol = short-gamma fragility, where a small shock forces big liquidation)."""
    s = (
        _ramp(payload["margin_debt_yoy"], 0.10, 0.50) * 0.45
        + _ramp(-payload["credit_balance_tn"], 0.40, 1.10) * 0.35   # more negative = more levered
        + _ramp(22 - payload["vxn_level"], 0, 12) * 0.20            # VXN 22->10 suppressed = fragile
    )
    return round(100 * s, 1)


def score_crowding(payload: dict) -> float:
    """High when the index IS the crowded trade (concentration) and everything
    moves as one (correlation)."""
    s = (
        _ramp(payload["top10_pct_of_ndx"], 0.40, 0.65) * 0.55
        + _ramp(payload["basket_correlation"], 0.40, 0.85) * 0.45
    )
    return round(100 * s, 1)


def score(payload: dict, prior_payload: dict | None = None):
    """Returns (band, signal, reasons, calc_payload) - matches the shape the
    orchestrator already expects from bear_indicator.score()."""
    breadth_divergence = compute_breadth_divergence(payload, prior_payload)
    a1 = score_stretch(payload, breadth_divergence)
    a2 = score_leverage(payload)
    a3 = score_crowding(payload)

    composite = round(
        a1 * AXIS_WEIGHTS["stretch"] + a2 * AXIS_WEIGHTS["leverage"] + a3 * AXIS_WEIGHTS["crowding"], 1
    )
    hot_axes = int(a1 > AXIS_HOT) + int(a2 > AXIS_HOT) + int(a3 > AXIS_HOT)

    if composite >= BAND_REDUCE and hot_axes >= MIN_HOT_AXES_FOR_RED:
        band, signal = "reduce", "🔴 REDUCE — fragile top"
    elif composite >= BAND_ELEVATED:
        band, signal = "elevated", "🟠 Elevated FOMO"
    elif composite >= BAND_BUILDING:
        band, signal = "building", "🟡 Building"
    else:
        band, signal = "green", "🟢 No euphoria"

    reasons = [
        f"Stretch: {payload['pct_ndx_over_2sigma']*100:.1f}% of NDX names >2σ above 50D mean, "
        f"{payload['pct_ndx_over_200dma']*100:.1f}% above 200DMA, RSI(14) {payload['ndx_rsi14']:.1f}"
        + (", breadth diverging (index near 20D high, participation falling)" if breadth_divergence else ""),
        f"Leverage: margin debt {'+' if payload['margin_debt_yoy'] >= 0 else ''}{payload['margin_debt_yoy']*100:.1f}% YoY, "
        f"net investor credit {payload['credit_balance_tn']:.2f}$T, VXN {payload['vxn_level']:.1f} "
        f"(as of {payload['margin_data_asof']} for margin/credit)",
        f"Crowding: top 10 NDX names = {payload['top10_pct_of_ndx']*100:.1f}% of index weight, "
        f"AI-hardware basket correlation {payload['basket_correlation']:.2f}",
    ]
    if hot_axes >= MIN_HOT_AXES_FOR_RED:
        reasons.append(f"{hot_axes} of 3 axes individually above the {AXIS_HOT} hot threshold")

    calc_payload = {
        "axis_stretch": a1,
        "axis_leverage": a2,
        "axis_crowding": a3,
        "score": composite,
        "band": band,
        "hot_axes": hot_axes,
        "breadth_divergence": breadth_divergence,
        "action": _ACTIONS[band],
    }
    return band, signal, reasons, calc_payload
