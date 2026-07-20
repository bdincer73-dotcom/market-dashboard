"""
Rules engine: turns the four R1 envelopes (breadth, sectors, volatility,
rates) into a single GREEN / AMBER / RED regime call, per the blueprint's
"Decision layer" section. Thresholds live in config/thresholds.yaml so you
can retune VIX bands, breadth levels, etc. without touching this code.

Priority: RED conditions are checked first (protect cash wins ties), then
AMBER, else GREEN. Every trigger is recorded in `reasons` so the dashboard
can show *why*, not just the color - this is the audit trail the blueprint
asks for ("every alert explains the rule, input values, and why it changed").

Any FAILED upstream envelope makes a confident regime call impossible, so we
downgrade to AMBER with an explicit "insufficient data" reason rather than
guessing GREEN.
"""

CALC_VERSION = "r1-scoring-v1"


def _delta(current, previous):
    if current is None or previous is None:
        return None
    return round(current - previous, 2)


def score(breadth_env, sectors_env, vol_env, rates_env, prev_breadth, prev_sectors, thresholds: dict):
    reasons = []
    failed_modules = [
        e.module for e in (breadth_env, sectors_env, vol_env, rates_env) if e.status == "FAILED"
    ]

    b = thresholds["breadth"]
    s = thresholds["sector_rotation"]
    v = thresholds["volatility"]

    # ---- breadth deltas vs previous run -----------------------------------
    d20 = d50 = d200 = None
    if breadth_env.status != "FAILED" and prev_breadth is not None:
        pp = prev_breadth["payload"]
        cp = breadth_env.payload
        d20 = _delta(cp.get("pct_above_20"), pp.get("pct_above_20"))
        d50 = _delta(cp.get("pct_above_50"), pp.get("pct_above_50"))
        d200 = _delta(cp.get("pct_above_200"), pp.get("pct_above_200"))

    # ---- sector/RSP confirmation -------------------------------------------
    rsp_vs_spy_5d = sectors_env.payload.get("rsp_vs_spy_5d") if sectors_env.status != "FAILED" else None
    sector_list = sectors_env.payload.get("sectors", []) if sectors_env.status != "FAILED" else []
    top_n = sorted([sec for sec in sector_list if sec.get("rank")], key=lambda x: x["rank"])[
        : s["narrow_leadership_rank_threshold"]
    ]
    narrow_leadership = bool(top_n) and all(sec.get("is_defensive") for sec in top_n)

    # ---- volatility ---------------------------------------------------------
    vix = vol_env.payload.get("VIX", {}) if vol_env.status != "FAILED" else {}
    vix_level = vix.get("level")
    vix_5d_change = vix.get("change_5d")
    curve_state = vol_env.payload.get("curve_state") if vol_env.status != "FAILED" else None
    is_backwardation = curve_state == "backwardation (stress)"

    # =========================== RED checks ==================================
    red = False
    if d20 is not None and d50 is not None:
        if d20 <= b["deteriorating_delta_max"] and d50 <= b["deteriorating_delta_max"]:
            red = True
            reasons.append(
                f"RED: 20D breadth Δ{d20} and 50D breadth Δ{d50} both deteriorating "
                f"(both ≤ {b['deteriorating_delta_max']} threshold)"
            )
    if d200 is not None and d200 < 0:
        red = True
        reasons.append(f"RED: 200D breadth weakening (Δ{d200}, any decline triggers this)")
    if rsp_vs_spy_5d is not None and rsp_vs_spy_5d < s["rsp_underperform_5d"]:
        red = True
        reasons.append(
            f"RED: RSP underperforming SPY over 5D "
            f"({rsp_vs_spy_5d} pts < {s['rsp_underperform_5d']} threshold)"
        )
    if vix_level is not None and vix_level > v["elevated_high"]:
        red = True
        reasons.append(f"RED: VIX elevated at {vix_level} (> {v['elevated_high']} threshold)")
    if is_backwardation:
        red = True
        reasons.append("RED: VIX term structure in backwardation (VIX3M < VIX - a stress signal)")

    if red:
        regime = "RED"

    else:
        # =========================== AMBER checks =============================
        amber = False
        if d20 is not None and (b["deteriorating_delta_max"] < d20 < b["stable_delta_min"]):
            amber = True
            reasons.append(
                f"AMBER: breadth mixed (20D Δ{d20}, between the "
                f"{b['deteriorating_delta_max']}/{b['stable_delta_min']} stable-vs-deteriorating bounds)"
            )
        if vix_5d_change is not None and vix_5d_change >= v["rising_5d"]:
            amber = True
            reasons.append(
                f"AMBER: VIX rising sharply over 5D (Δ{vix_5d_change} ≥ {v['rising_5d']} threshold)"
            )
        if narrow_leadership:
            amber = True
            reasons.append(
                f"AMBER: sector leadership narrow/defensive "
                f"(top {s['narrow_leadership_rank_threshold']} ranked sectors are all defensive)"
            )
        if vix_level is not None and not (v["calm_low"] <= vix_level <= v["calm_high"]):
            amber = True
            reasons.append(
                f"AMBER: VIX outside calm band ({vix_level}, calm range is "
                f"{v['calm_low']}–{v['calm_high']})"
            )
        if failed_modules:
            amber = True
            reasons.append(f"AMBER: insufficient data - failed module(s) {failed_modules}")
        if prev_breadth is None:
            amber = True
            reasons.append("AMBER: no prior breadth snapshot yet - deltas unavailable on first run")

        regime = "AMBER" if amber else "GREEN"
        if regime == "GREEN":
            reasons.append(
                "GREEN: breadth stable/rising, RSP confirming, VIX calm, leadership broad "
                "- no rule tripped"
            )

    payload = {
        "breadth_delta_20d": d20,
        "breadth_delta_50d": d50,
        "breadth_delta_200d": d200,
        "rsp_vs_spy_5d": rsp_vs_spy_5d,
        "narrow_leadership": narrow_leadership,
        "vix_level": vix_level,
        "vix_5d_change": vix_5d_change,
        "curve_state": curve_state,
        "failed_modules": failed_modules,
    }

    return regime, reasons, payload
