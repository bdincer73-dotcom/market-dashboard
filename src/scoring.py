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

Every threshold used below comes from config/thresholds.yaml - as of this
version, that now includes ALL of the breadth/sector_rotation/volatility
keys defined there (weak_20d_level, weak_50d_level, weak_200d_level,
min_coverage_pct, rsp_confirm_5d, backwardation_threshold used to be defined
in the YAML but silently never read by this file - fixed below, see each
check's comment for what it now does).
"""

CALC_VERSION = "r1-scoring-v2"


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

    # ---- breadth levels + deltas vs previous run ---------------------------
    # Levels are pulled independent of prev_breadth so the level-based checks
    # below (weak_20d_level etc.) still work on the very first run, before
    # any delta can be computed.
    pct20 = pct50 = pct200 = coverage_pct = None
    if breadth_env.status != "FAILED":
        cp = breadth_env.payload
        pct20 = cp.get("pct_above_20")
        pct50 = cp.get("pct_above_50")
        pct200 = cp.get("pct_above_200")
        coverage_pct = cp.get("coverage_pct")

    d20 = d50 = d200 = None
    if breadth_env.status != "FAILED" and prev_breadth is not None:
        pp = prev_breadth["payload"]
        d20 = _delta(pct20, pp.get("pct_above_20"))
        d50 = _delta(pct50, pp.get("pct_above_50"))
        d200 = _delta(pct200, pp.get("pct_above_200"))

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
    # Derived from the raw VIX3M-VIX diff against the configured threshold,
    # NOT from curve_state's string label - the collector's label is only a
    # display convenience (always splits at 0) and used to be the only thing
    # this check read, which meant `backwardation_threshold` in
    # thresholds.yaml was decorative. Reading the raw number here is what
    # actually makes that config value tunable.
    vix3m_minus_vix = vol_env.payload.get("vix3m_minus_vix") if vol_env.status != "FAILED" else None
    is_backwardation = vix3m_minus_vix is not None and vix3m_minus_vix <= v["backwardation_threshold"]

    # =========================== RED checks ==================================
    red = False
    if d20 is not None and d50 is not None:
        if d20 <= b["deteriorating_delta_max"] and d50 <= b["deteriorating_delta_max"]:
            red = True
            reasons.append(
                f"RED: 20D breadth Δ{d20} and 50D breadth Δ{d50} both deteriorating "
                f"(both ≤ {b['deteriorating_delta_max']} threshold)"
            )
    # 200D delta now uses the same "deteriorating" magnitude as 20D/50D
    # instead of firing on any decline at all (even -0.1pp) - that old
    # behavior made this by far the most common RED trigger in practice,
    # well out of proportion to how noisy a single day's %-above-200DMA
    # reading can be.
    if d200 is not None and d200 <= b["deteriorating_delta_max"]:
        red = True
        reasons.append(
            f"RED: 200D breadth weakening (Δ{d200} ≤ {b['deteriorating_delta_max']} threshold)"
        )
    # Level-based check (previously unused weak_200d_level): if the *level*
    # itself is already below the "broadly weak" bar, that's RED regardless
    # of this week's delta - matches the pattern already used for VIX below
    # (a level threshold, not just a delta), and stays RED persistently while
    # the majority of names remain below their 200DMA rather than only firing
    # the day it first crosses.
    if pct200 is not None and pct200 < b["weak_200d_level"]:
        red = True
        reasons.append(
            f"RED: 200D breadth level weak ({pct200}% < {b['weak_200d_level']}% threshold) - "
            f"majority of names below their 200DMA, independent of this week's move"
        )
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
        reasons.append(
            f"RED: VIX term structure in backwardation (VIX3M-VIX {vix3m_minus_vix} ≤ "
            f"{v['backwardation_threshold']} threshold - a stress signal)"
        )

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
        # Previously-unused weak_20d_level/weak_50d_level: short-term breadth
        # already soft in absolute terms, even without a sharp weekly drop.
        if pct20 is not None and pct50 is not None and pct20 < b["weak_20d_level"] and pct50 < b["weak_50d_level"]:
            amber = True
            reasons.append(
                f"AMBER: short-term breadth soft (20D {pct20}% < {b['weak_20d_level']}%, "
                f"50D {pct50}% < {b['weak_50d_level']}%)"
            )
        # Previously-unused min_coverage_pct: a low-coverage breadth reading
        # (constituent data mostly missing) is less trustworthy, so it should
        # downgrade confidence the same way a FAILED module does.
        if coverage_pct is not None and coverage_pct < b["min_coverage_pct"]:
            amber = True
            reasons.append(
                f"AMBER: breadth coverage below quality gate ({coverage_pct}% < "
                f"{b['min_coverage_pct']}%) - reading less reliable this run"
            )
        # Previously-unused rsp_confirm_5d: RSP isn't underperforming enough
        # to trip the RED check above, but it isn't confirming the rally
        # either - a soft/neutral zone that used to default straight to
        # GREEN (rsp_underperform_5d <= rsp_vs_spy_5d < rsp_confirm_5d).
        if rsp_vs_spy_5d is not None and s["rsp_underperform_5d"] <= rsp_vs_spy_5d < s["rsp_confirm_5d"]:
            amber = True
            reasons.append(
                f"AMBER: RSP not yet confirming the rally ({rsp_vs_spy_5d} pts, below the "
                f"{s['rsp_confirm_5d']} confirm threshold but not underperforming)"
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
        "pct_above_20": pct20,
        "pct_above_50": pct50,
        "pct_above_200": pct200,
        "coverage_pct": coverage_pct,
        "rsp_vs_spy_5d": rsp_vs_spy_5d,
        "narrow_leadership": narrow_leadership,
        "vix_level": vix_level,
        "vix_5d_change": vix_5d_change,
        "curve_state": curve_state,
        "vix3m_minus_vix": vix3m_minus_vix,
        "failed_modules": failed_modules,
    }

    return regime, reasons, payload
