"""Shared helpers for the PDPC fragment-backfill pipelines.

All three resources (enforcement_decisions, guidance_by_topic, regulatory_guidance)
run a fragment backfill that extracts PDF text via a Docling server and tracks
failures in a JSON checkpoint of the shape::

    {item_id: {"failures": int, "last_error": str, "last_attempt": iso8601}}

Historically, items whose failure count reached the retry cap were quarantined
FOREVER — a one-way door. Most failures are transient (proxy down, Docling
timeout), so this module adds a quarantine TTL: quarantined items become
eligible again once their last_attempt is older than
``PDPC_BACKFILL_RETRY_AFTER`` seconds (default 14 days ≈ two weekly builds).
The failure count is preserved, so a still-broken item re-quarantines after a
single failed re-attempt and waits another TTL window.

Env vars (shared by all three resources):

- ``PDPC_BACKFILL_RETRY_AFTER`` — quarantine TTL in seconds (default 1209600).
- ``PDPC_BACKFILL_RETRY_QUARANTINED=1`` — manual flush: ALL quarantined items
  become eligible this run, regardless of TTL.

The functions here are pure (time and env are passed in or injectable) so they
can be unit-tested without touching the network or the filesystem.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

#: Default quarantine TTL: 14 days, i.e. roughly two weekly builds.
DEFAULT_RETRY_AFTER_SECONDS = 1209600

RETRY_AFTER_ENV = "PDPC_BACKFILL_RETRY_AFTER"
RETRY_QUARANTINED_ENV = "PDPC_BACKFILL_RETRY_QUARANTINED"

#: Minimum extracted-PDF length treated as a successful extraction. Shared by
#: fetch_data() and the backfill so both apply the same success threshold.
MIN_PDF_TEXT_CHARS = 50


def get_retry_after_seconds(env: Optional[Mapping[str, str]] = None) -> int:
    """Read the quarantine TTL from the environment (seconds).

    Falls back to DEFAULT_RETRY_AFTER_SECONDS when unset, non-numeric,
    or negative.
    """
    env = os.environ if env is None else env
    raw = env.get(RETRY_AFTER_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    return value if value >= 0 else DEFAULT_RETRY_AFTER_SECONDS


def get_retry_quarantined(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when PDPC_BACKFILL_RETRY_QUARANTINED=1 (manual quarantine flush)."""
    env = os.environ if env is None else env
    return env.get(RETRY_QUARANTINED_ENV, "") == "1"


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp; return None on missing/malformed input.

    Naive timestamps are assumed to be UTC.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def is_quarantined(record: Optional[Dict[str, Any]], max_retries: int) -> bool:
    """True when a checkpoint record has reached the failure cap."""
    if not record:
        return False
    return record.get("failures", 0) >= max_retries


def is_eligible(
    record: Optional[Dict[str, Any]],
    max_retries: int,
    now: datetime,
    retry_after_seconds: int,
    retry_quarantined: bool = False,
) -> bool:
    """Decide whether a backfill candidate may be attempted this run.

    - No checkpoint record, or failures below the cap: eligible.
    - Quarantined (failures >= cap):
      - eligible when ``retry_quarantined`` is set (manual flush);
      - eligible when ``last_attempt`` is missing or malformed (fail open —
        never leave an item stuck forever on bad checkpoint data);
      - otherwise eligible only once ``last_attempt`` is at least
        ``retry_after_seconds`` old.
    """
    if not is_quarantined(record, max_retries):
        return True
    if retry_quarantined:
        return True
    last_attempt = parse_timestamp(record.get("last_attempt")) if record else None
    if last_attempt is None:
        return True
    return (now - last_attempt).total_seconds() >= retry_after_seconds


def format_error(exc: BaseException, max_message_len: int = 160) -> str:
    """Render an exception as 'TypeName: message', truncating only the message.

    The exception class name is never truncated — it is the most useful part
    of a failure line for build monitoring.
    """
    cls = type(exc).__name__
    msg = str(exc)
    if len(msg) > max_message_len:
        msg = msg[: max_message_len - 1] + "…"
    return f"{cls}: {msg}" if msg else cls


def compute_still_pending(total_candidates: int, attempted: int, failed_retryable: int) -> int:
    """True still-pending count after a backfill batch.

    ``total_candidates`` — eligible candidates at the start of the run.
    ``attempted`` — items actually processed this batch.
    ``failed_retryable`` — batch items that failed but are NOT quarantined
    (failure count still below the cap), so they WILL be retried next build.

    Still pending = un-attempted candidates + failed-but-retryable items.
    """
    return max(0, total_candidates - attempted) + failed_retryable
