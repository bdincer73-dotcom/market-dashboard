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

from src import store, scoring, publish, candidate_scoring
from src.collectors import breadth, sectors, volatility, rates, news, bear_indicator, earnings, options_chain

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

    core_envelopes = [breadth_env, sectors_env, vol_env, rates_env, news_env, earnings_env, options_env]
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
    if run_type == "weekly" and breadth_env.status != "FAILED":
        bear_env = bear_indicator.collect(breadth_env.payload)
        print(f"  - {bear_env.module}: {bear_env.status} (obs {bear_env.observation_date}) {bear_env.notes}")

    with store.connect() as conn:
        snapshot_ids = []
        prev_breadth = store.get_previous_snapshot(conn, "breadth", run_id)
        prev_sectors = store.get_previous_snapshot(conn, "sectors", run_id)
        prev_bear = store.get_previous_snapshot(conn, "bear_indicator", run_id) if bear_env else None

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

    health_envelopes = [breadth_env, sectors_env, vol_env, rates_env, news_env, earnings_env, options_env]
    if bear_env is not None:
        health_envelopes.append(bear_env)

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
        "news": news_env,
        "earnings": earnings_env,
        "options_chain": options_env,
        "candidates": candidates,
        "bear_indicator": bear_env,
        "bear_score": bear_score,
        "bear_signal": bear_signal,
        "bear_reasons": bear_reasons,
        "bear_icon": bear_indicator.SIGNAL_ICON.get(bear_signal) if bear_signal else None,
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
