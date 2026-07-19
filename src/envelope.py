"""
Common envelope every collector returns. Keeping this shape identical across
modules is what makes the storage layer, quality gates, and dashboard footer
generic instead of one-off per source.

status meanings (per the blueprint's freshness quality gate):
  LIVE      - retrieved during today's session, market data intraday.
  EOD       - retrieved after today's close, reflects today's completed session.
  STALE     - retrieved, but the observation_date is older than expected
              (e.g. feed returned yesterday's bar, or a key is missing so we
              fell back to a cached value).
  FAILED    - the collector could not get a usable value at all. Never
              carry forward an unlabeled number - payload should be {} on FAILED.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


VALID_STATUSES = {"LIVE", "EOD", "STALE", "FAILED"}


@dataclass
class Envelope:
    module: str                     # e.g. "breadth", "sectors", "volatility", "rates"
    source: str                     # e.g. "yfinance", "FRED"
    retrieved_at: str               # ISO8601 UTC timestamp of the API call
    observation_date: Optional[str] # the trading/data date the values represent, or None
    status: str                     # one of VALID_STATUSES
    payload: dict = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self):
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid status {self.status!r}, must be one of {VALID_STATUSES}")
        if self.status == "FAILED" and self.payload:
            # Keep the invariant simple: failed means no numbers, full stop.
            raise ValueError("FAILED envelopes must not carry a payload")

    def to_dict(self) -> dict:
        return asdict(self)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def failed(module: str, source: str, notes: str) -> Envelope:
    return Envelope(
        module=module,
        source=source,
        retrieved_at=now_utc_iso(),
        observation_date=None,
        status="FAILED",
        payload={},
        notes=notes,
    )
