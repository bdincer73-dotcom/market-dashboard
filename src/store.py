"""
SQLite archive. Two tables, per the blueprint's "raw data and calculated
signals remain separate so every number is traceable" principle:

  raw_snapshots       - one row per collector call, immutable, never updated.
  calculated_signals  - one row per scoring run, references the snapshot ids
                        it was computed from, so any historical dashboard
                        can be reproduced exactly.

Graduates to S3/DynamoDB later (R3) if concurrency/history needs outgrow a
single file - not needed for a once/twice-a-day job.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market_dashboard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    module          TEXT NOT NULL,
    source          TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    observation_date TEXT,
    status          TEXT NOT NULL,
    notes           TEXT,
    payload_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_module_date
    ON raw_snapshots (module, retrieved_at);

CREATE TABLE IF NOT EXISTS calculated_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    run_type        TEXT NOT NULL,   -- 'daily' or 'weekly'
    calculated_at   TEXT NOT NULL,
    calc_version    TEXT NOT NULL,
    snapshot_ids    TEXT NOT NULL,   -- JSON list of raw_snapshots.id this run used
    regime          TEXT NOT NULL,   -- GREEN / AMBER / RED
    reasons_json    TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_snapshot(conn, run_id: str, envelope) -> int:
    cur = conn.execute(
        """INSERT INTO raw_snapshots
           (run_id, module, source, retrieved_at, observation_date, status, notes, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            envelope.module,
            envelope.source,
            envelope.retrieved_at,
            envelope.observation_date,
            envelope.status,
            envelope.notes,
            json.dumps(envelope.payload),
        ),
    )
    return cur.lastrowid


def get_previous_snapshot(conn, module: str, before_run_id: str):
    """Most recent non-failed snapshot for `module` strictly before this run,
    used to compute 1D deltas and rank changes."""
    row = conn.execute(
        """SELECT * FROM raw_snapshots
           WHERE module = ? AND run_id < ? AND status != 'FAILED'
           ORDER BY run_id DESC LIMIT 1""",
        (module, before_run_id),
    ).fetchone()
    if row is None:
        return None
    return {**dict(row), "payload": json.loads(row["payload_json"])}


def save_calculated(conn, run_id: str, run_type: str, calc_version: str,
                     snapshot_ids: list, regime: str, reasons: list, payload: dict):
    conn.execute(
        """INSERT INTO calculated_signals
           (run_id, run_type, calculated_at, calc_version, snapshot_ids, regime, reasons_json, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            run_type,
            envelope_now(),
            calc_version,
            json.dumps(snapshot_ids),
            regime,
            json.dumps(reasons),
            json.dumps(payload),
        ),
    )


def envelope_now() -> str:
    from src.envelope import now_utc_iso
    return now_utc_iso()


def get_latest_calculated(conn, calc_version: str):
    """Most recent calculated_signals row for a given calc_version, regardless
    of run_type. Used to carry forward the weekly-only Bear Indicator reading
    onto daily dashboards instead of it disappearing between Saturdays."""
    row = conn.execute(
        """SELECT * FROM calculated_signals WHERE calc_version = ?
           ORDER BY run_id DESC LIMIT 1""",
        (calc_version,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["reasons"] = json.loads(d["reasons_json"])
    d["payload"] = json.loads(d["payload_json"])
    return d


def get_snapshot_history(conn, module: str, limit: int = 10):
    rows = conn.execute(
        """SELECT * FROM raw_snapshots WHERE module = ? AND status != 'FAILED'
           ORDER BY run_id DESC LIMIT ?""",
        (module, limit),
    ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]
