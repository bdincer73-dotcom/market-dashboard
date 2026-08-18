"""
Orchestrator: source -> validate -> normalize -> calculate -> score -> publish -> archive.

Usage:
    python -m src.main --run-type daily
    python -m src.main --run-type weekly

Exit code is non-zero only if a run could not produce ANY dashboard at all
(e.g. templating crashed) - individual FAILED modules degrade the regime to
AMBER but still produce a dashboard, since a partial dashboard with honest
STALE/FAILED badges is more useful than no dashboard.
"""

import argparse
import sys
from datetime import datetime, timezone

import yaml

from src import store, scoring, publish, candidate_scoring, fomo_scoring, fed_liquidity_scoring
from src.collectors import (
    breadth, sectors, volatility, rates, news, bear_indicator, earnings,
    options_chain, fomo_fragility, consumer_sentiment, fed_liquidity,
)
from src.envelope import Envelope

CONFIG_PATH = __file__.replace("src/main.py", "config/thresholds.yaml")


def load_thresholds() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def build_module_health(envelopes: list) -> list:
    return [
        {
            "module": e.module,
            "source": e.source,
            "status": e.status,
            "observation_date": e.observation_date,
            "retrieved_at": e.retrieved_at,
        }
        for e in envelopes
    ]


def run(run_type: str) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    thresholds = load_thresholds()

    print(f"[{run_id}] collecting ({run_type}) ...")
    breadth_env = breadth.collect()
    sectors_env = sectors.collect()
    vol_env = volatility.collect()
    rates_env = rates.collect()
    news_env = news.collect()
    earnings_env = earnings.collect()
    options_env = options_chain.collect()
    sentiment_env = consumer_sentiment.collect()

    core_envelopes = [breadth_env, sectors_env, vol_env, rates_env, news_env, earnings_env, options_env, sentiment_env]
    for e in core_envelopes:
        print(f"  - {e.module}: {e.status} (obs {e.observation_date}) {e.notes}")

    # R2 candidate scoring - needs both earnings and options chain data.
    candidates = None
    if earnings_env.status != "FAILED" and options_env.status != "FAILED":
        candidates = candidate_scoring.score_all(options_env.payload, earnings_env.payload, thresholds)
        print(
            f"  - candidates: {candidates['candidate_count']} pass all gates, "
            f"{candidates['watch_only_count']} watch-only"
        )

    # Bear Indicator is weekly-cadence by design (ported from a weekly-updated
    # tracker) - only collected/scored on the Saturday run, not every day.
    bear_env = None
    bear_score = bear_signal = None
    bear_reasons = []
    bear_as_of = None
    if run_type == "weekly" and breadth_env.status != "FAILED":
        bear_env = bear_indicator.collect(breadth_env.payload)
        bear_as_of = run_date
        print(f"  - {bear_env.module}: {bear_env.status} (obs {bear_env.observation_date}) {bear_env.notes}")

    # FOMO Fragility Index - companion euphoria/top indicator to the Bear
    # Indicator, same weekly-only cadence and same carry-forward pattern.
    fomo_env = None
    fomo_band = fomo_signal = None
    fomo_reasons = []
    fomo_calc = {}
    fomo_as_of = None
    if run_type == "weekly":
        fomo_env = fomo_fragility.collect()
        fomo_as_of = run_date
        print(f"  - {fomo_env.module}: {fomo_env.status} (obs {fomo_env.observation_date}) {fomo_env.notes}")

    # Fed Liquidity Indicator - macro overlay for CSP sizing, same weekly-only
    # cadence and carry-forward pattern as the Bear Indicator / FOMO above.
    # Underlying FRED series (WALCL/WTREGEN/WRESBAL) only print weekly
    # (Wednesday) anyway, so a daily recompute would just re-fetch the same
    # numbers.
    fedliq_env = None
    fedliq_score = fedliq_regime = None
    fedliq_reasons = []
    fedliq_calc = {}
    fedliq_as_of = None
    if run_type == "weekly":
        fedliq_env = fed_liquidity.collect()
        fedliq_as_of = run_date
        print(f"  - {fedliq_env.module}: {fedliq_env.status} (obs {fedliq_env.observation_date}) {fedliq_env.notes}")

    with store.connect() as conn:
        snapshot_ids = []
        prev_breadth = store.get_previous_snapshot(conn, "breadth", run_id)
        prev_sectors = store.get_previous_snapshot(conn, "sectors", run_id)
        prev_bear = store.get_previous_snapshot(conn, "bear_indicator", run_id) if bear_env else None
        prev_fomo = store.get_previous_snapshot(conn, "fomo_fragility", run_id) if fomo_env else None

        for e in core_envelopes:
            snapshot_ids.append(store.save_snapshot(conn, run_id, e))

        regime, reasons, calc_payload = scoring.score(
            breadth_env, sectors_env, vol_env, rates_env,
            prev_breadth, prev_sectors, thresholds,
        )

        store.save_calculated(
            conn, run_id, run_type, scoring.CALC_VERSION,
            snapshot_ids, regime, reasons, calc_payload,
        )

        if bear_env is not None:
            bear_snapshot_id = store.save_snapshot(conn, run_id, bear_env)
            if bear_env.status != "FAILED":
                bear_score, bear_signal, bear_reasons = bear_indicator.score(
                    bear_env.payload, prev_bear["payload"] if prev_bear else None
                )
                store.save_calculated(
                    conn, run_id, run_type, "bear-indicator-v1",
                    [bear_snapshot_id], bear_signal, bear_reasons,
                    {"score": bear_score, **bear_env.payload},
                )
        else:
            # Not a weekly run (or weekly breadth failed) - carry the most
            # recent weekly reading forward instead of letting the card
            # disappear from the dashboard between Saturdays. Marked STALE
            # (not EOD) since it's not fresh for *this* run, with a clear
            # as-of date so it's never mistaken for a live update.
            carried_snapshot = store.get_previous_snapshot(conn, "bear_indicator", run_id)
            carried_calc = store.get_latest_calculated(conn, "bear-indicator-v1")
            if carried_snapshot and carried_calc:
                bear_env = Envelope(
                    module=carried_snapshot["module"],
                    source=carried_snapshot["source"],
                    retrieved_at=carried_snapshot["retrieved_at"],
                    observation_date=carried_snapshot["observation_date"],
                    status="STALE",
                    payload=carried_snapshot["payload"],
                    notes=(
                        f"Carried forward - Bear Indicator only recomputes on the weekly "
                        f"(Saturday) run. Next fresh reading this coming Saturday."
                    ),
                )
                bear_score = carried_calc["payload"]["score"]
                bear_signal = carried_calc["regime"]
                bear_reasons = carried_calc["reasons"]
                bear_as_of = carried_calc["run_id"][:4] + "-" + carried_calc["run_id"][4:6] + "-" + carried_calc["run_id"][6:8]

        if fomo_env is not None:
            fomo_snapshot_id = store.save_snapshot(conn, run_id, fomo_env)
            if fomo_env.status != "FAILED":
                fomo_band, fomo_signal, fomo_reasons, fomo_calc = fomo_scoring.score(
                    fomo_env.payload, prev_fomo["payload"] if prev_fomo else None
                )
                store.save_calculated(
                    conn, run_id, run_type, fomo_scoring.CALC_VERSION,
                    [fomo_snapshot_id], fomo_signal, fomo_reasons, fomo_calc,
                )
        else:
            # Not a weekly run - carry the most recent weekly reading forward
            # (same rationale as the Bear Indicator carry-forward above).
            carried_fomo_snapshot = store.get_previous_snapshot(conn, "fomo_fragility", run_id)
            carried_fomo_calc = store.get_latest_calculated(conn, fomo_scoring.CALC_VERSION)
            if carried_fomo_snapshot and carried_fomo_calc:
                fomo_env = Envelope(
                    module=carried_fomo_snapshot["module"],
                    source=carried_fomo_snapshot["source"],
                    retrieved_at=carried_fomo_snapshot["retrieved_at"],
                    observation_date=carried_fomo_snapshot["observation_date"],
                    status="STALE",
                    payload=carried_fomo_snapshot["payload"],
                    notes=(
                        f"Carried forward - FOMO Fragility Index only recomputes on the weekly "
                        f"(Saturday) run. Next fresh reading this coming Saturday."
                    ),
                )
                fomo_band = carried_fomo_calc["payload"]["band"]
                fomo_signal = carried_fomo_calc["regime"]
                fomo_reasons = carried_fomo_calc["reasons"]
                fomo_calc = carried_fomo_calc["payload"]
                fomo_as_of = (
                    carried_fomo_calc["run_id"][:4] + "-" + carried_fomo_calc["run_id"][4:6]
                    + "-" + carried_fomo_calc["run_id"][6:8]
                )

        if fedliq_env is not None:
            fedliq_snapshot_id = store.save_snapshot(conn, run_id, fedliq_env)
            if fedliq_env.status != "FAILED":
                fedliq_score, fedliq_regime, fedliq_reasons, fedliq_calc = fed_liquidity_scoring.score(
                    fedliq_env.payload, thresholds.get("fed_liquidity")
                )
                store.save_calculated(
                    conn, run_id, run_type, fed_liquidity_scoring.CALC_VERSION,
                    [fedliq_snapshot_id], fedliq_regime, fedliq_reasons, fedliq_calc,
                )
        else:
            # Not a weekly run - carry the most recent weekly reading forward
            # (same rationale as the Bear Indicator / FOMO carry-forward above).
            carried_fedliq_snapshot = store.get_previous_snapshot(conn, "fed_liquidity", run_id)
            carried_fedliq_calc = store.get_latest_calculated(conn, fed_liquidity_scoring.CALC_VERSION)
            if carried_fedliq_snapshot and carried_fedliq_calc:
                fedliq_env = Envelope(
                    module=carried_fedliq_snapshot["module"],
                    source=carried_fedliq_snapshot["source"],
                    retrieved_at=carried_fedliq_snapshot["retrieved_at"],
                    observation_date=carried_fedliq_snapshot["observation_date"],
                    status="STALE",
                    payload=carried_fedliq_snapshot["payload"],
                    notes=(
                        f"Carried forward - Fed Liquidity Indicator only recomputes on the weekly "
                        f"(Saturday) run. Next fresh reading this coming Saturday."
                    ),
                )
                fedliq_score = carried_fedliq_calc["payload"]["score"]
                fedliq_regime = carried_fedliq_calc["regime"]
                fedliq_reasons = carried_fedliq_calc["reasons"]
                fedliq_calc = carried_fedliq_calc["payload"]
                fedliq_as_of = (
                    carried_fedliq_calc["run_id"][:4] + "-" + carried_fedliq_calc["run_id"][4:6]
                    + "-" + carried_fedliq_calc["run_id"][6:8]
                )

        earnings_snapshot_id = store.save_snapshot(conn, run_id, earnings_env)
        options_snapshot_id = store.save_snapshot(conn, run_id, options_env)
        if candidates is not None:
            store.save_calculated(
                conn, run_id, run_type, candidate_scoring.CALC_VERSION,
                [earnings_snapshot_id, options_snapshot_id],
                f"{candidates['candidate_count']}_candidates",
                [c["ticker"] for c in candidates["candidates"]],
                candidates,
            )

    breadth_deltas = {
        "d20": calc_payload.get("breadth_delta_20d"),
        "d50": calc_payload.get("breadth_delta_50d"),
        "d200": calc_payload.get("breadth_delta_200d"),
    }

    health_envelopes = [breadth_env, sectors_env, vol_env, rates_env, news_env, earnings_env, options_env, sentiment_env]
    if bear_env is not None:
        health_envelopes.append(bear_env)
    if fomo_env is not None:
        health_envelopes.append(fomo_env)
    if fedliq_env is not None:
        health_envelopes.append(fedliq_env)

    context = {
        "run_type": run_type,
        "run_date": run_date,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "regime": regime,
        "regime_label": publish.REGIME_LABELS[regime],
        "reasons": reasons,
        "breadth": breadth_env,
        "breadth_deltas": breadth_deltas,
        "sectors": sectors_env,
        "volatility": vol_env,
        "rates": rates_env,
        "consumer_sentiment": sentiment_env,
        "news": news_env,
        "earnings": earnings_env,
        "options_chain": options_env,
        "candidates": candidates,
        "bear_indicator": bear_env,
        "bear_score": bear_score,
        "bear_signal": bear_signal,
        "bear_reasons": bear_reasons,
        "bear_as_of": bear_as_of,
        "bear_icon": bear_indicator.SIGNAL_ICON.get(bear_signal) if bear_signal else None,
        "fomo_indicator": fomo_env,
        "fomo_band": fomo_band,
        "fomo_signal": fomo_signal,
        "fomo_reasons": fomo_reasons,
        "fomo_calc": fomo_calc,
        "fomo_as_of": fomo_as_of,
        "fed_liquidity": fedliq_env,
        "fedliq_score": fedliq_score,
        "fedliq_regime": fedliq_regime,
        "fedliq_reasons": fedliq_reasons,
        "fedliq_calc": fedliq_calc,
        "fedliq_as_of": fedliq_as_of,
        "module_health": build_module_health(health_envelopes),
        "calc_version": scoring.CALC_VERSION,
    }

    paths = publish.publish(context, run_type, run_date)
    print(f"[{run_id}] regime={regime}  dashboard={paths['latest']}")
    return {"run_id": run_id, "regime": regime, **paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-type", choices=["daily", "weekly"], default="daily")
    args = parser.parse_args()
    try:
        run(args.run_type)
    except Exception as exc:
        print(f"FATAL: dashboard run failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
