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

from src import store, scoring, publish
from src.collectors import breadth, sectors, volatility, rates

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

    for e in (breadth_env, sectors_env, vol_env, rates_env):
        print(f"  - {e.module}: {e.status} (obs {e.observation_date}) {e.notes}")

    with store.connect() as conn:
        snapshot_ids = []
        prev_breadth = store.get_previous_snapshot(conn, "breadth", run_id)
        prev_sectors = store.get_previous_snapshot(conn, "sectors", run_id)

        for e in (breadth_env, sectors_env, vol_env, rates_env):
            snapshot_ids.append(store.save_snapshot(conn, run_id, e))

        regime, reasons, calc_payload = scoring.score(
            breadth_env, sectors_env, vol_env, rates_env,
            prev_breadth, prev_sectors, thresholds,
        )

        store.save_calculated(
            conn, run_id, run_type, scoring.CALC_VERSION,
            snapshot_ids, regime, reasons, calc_payload,
        )

    breadth_deltas = {
        "d20": calc_payload.get("breadth_delta_20d"),
        "d50": calc_payload.get("breadth_delta_50d"),
        "d200": calc_payload.get("breadth_delta_200d"),
    }

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
        "module_health": build_module_health([breadth_env, sectors_env, vol_env, rates_env]),
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
