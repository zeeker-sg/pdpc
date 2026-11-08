"""Re-parse detail pages for existing rows that were captured with missing fields.

Rows are insert-only: a record is parsed once, at discovery, and never re-fetched.
When a parser regression ships (PDPC moved its Next.js RSC rows; guidance
documents moved into a banner ``cta.url``), the rows captured meanwhile keep
their empty ``summary`` / ``pdf_url`` forever. This backfill re-fetches the
detail page for such rows, re-parses it with the current parser, and fills in
the blank fields only, never overwriting a value that is already set. The
existing fragment backfill then turns any newly found ``pdf_url`` into
fragments (it runs right after this one inside the same build).

Each resource calls :func:`refill_from_detail_pages` from ``fetch_data()`` as a
build side effect, the same way the fragment backfill runs. Progress lives in a
JSON checkpoint ``{id: {"checked": iso8601} | {"failures", "last_error",
"last_attempt"}}``:

- a page that was fetched and parsed is marked ``checked`` and not fetched again,
  even when it yields nothing new (e.g. a training page whose CTA is an external
  site), unless ``PDPC_DETAIL_BACKFILL_RECHECK=1``;
- a page that failed to fetch counts a failure and follows the same retry cap and
  quarantine TTL as the fragment backfill (see ``_backfill``).

Env vars:

- ``PDPC_DETAIL_BACKFILL_BATCH_SIZE`` — pages re-fetched per resource per build
  (default 50; ``0`` disables the backfill).
- ``PDPC_DETAIL_BACKFILL_MAX_RETRIES`` — failure cap before quarantine (default 3).
- ``PDPC_DETAIL_BACKFILL_RECHECK=1`` — re-fetch rows already marked checked.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

import _backfill

BATCH_SIZE_ENV = "PDPC_DETAIL_BACKFILL_BATCH_SIZE"
MAX_RETRIES_ENV = "PDPC_DETAIL_BACKFILL_MAX_RETRIES"
RECHECK_ENV = "PDPC_DETAIL_BACKFILL_RECHECK"
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_RETRIES = 3


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def fill_blanks(row: Mapping[str, Any], parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """Updates that fill ``row``'s empty fields from ``parsed``; never overwrite.

    Only keys present in both are considered. "Empty" is None or "".
    """
    updates: Dict[str, Any] = {}
    for key, new in parsed.items():
        if key not in row:
            continue
        if row[key] in (None, "") and new not in (None, ""):
            updates[key] = new
    return updates


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def refill_from_detail_pages(
    db,
    *,
    table: str,
    url_column: str,
    where: str,
    parse: Callable[[str], Dict[str, Any]],
    fetch_text: Callable[[Any, str], str],
    make_client: Callable[[], Any],
    sleep: Callable[[], None],
    checkpoint_path: str,
    echo: Callable[..., None],
    report: Callable[..., None],
    env: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Re-fetch and re-parse the detail page of each row matching ``where``.

    ``parse`` maps page HTML to a dict keyed by column names (keys that are not
    columns are ignored). Returns the counters it also passes to ``report``.
    """
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    batch_size = _env_int(env, BATCH_SIZE_ENV, DEFAULT_BATCH_SIZE)
    max_retries = _env_int(env, MAX_RETRIES_ENV, DEFAULT_MAX_RETRIES)
    recheck = env.get(RECHECK_ENV, "") == "1"
    retry_after = _backfill.get_retry_after_seconds(env)
    retry_quarantined = _backfill.get_retry_quarantined(env)

    counts = {"details_filled": 0, "details_unchanged": 0, "details_failed": 0,
              "details_pending": 0}
    if batch_size == 0 or not db[table].exists():
        return counts

    checkpoint = _load(checkpoint_path)
    rows: List[Dict[str, Any]] = list(
        db[table].rows_where(f"({where}) AND {url_column} LIKE 'http%'", order_by="rowid")
    )
    candidates = []
    for row in rows:
        rec = checkpoint.get(row["id"])
        if rec and rec.get("checked") and not recheck:
            continue
        if rec and "failures" in rec and not _backfill.is_eligible(
            rec, max_retries, now, retry_after, retry_quarantined
        ):
            continue
        candidates.append(row)

    if not candidates:
        echo(f"{table}: Detail backfill: nothing to do.")
        report(counts)
        return counts

    batch = candidates[:batch_size]
    echo(
        f"{table}: Detail backfill: {len(candidates)} row(s) to re-parse, "
        f"processing {len(batch)} this run."
    )
    with make_client() as client:
        for row in batch:
            rid = row["id"]
            try:
                sleep()
                parsed = parse(fetch_text(client, row[url_column]))
            except Exception as e:  # noqa: BLE001 — every failure is recorded and retried
                rec = checkpoint.get(rid) or {}
                rec = {
                    "failures": rec.get("failures", 0) + 1,
                    "last_error": _backfill.format_error(e)[:200],
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                }
                checkpoint[rid] = rec
                _save(checkpoint_path, checkpoint)
                counts["details_failed"] += 1
                if rec["failures"] < max_retries:
                    counts["details_pending"] += 1
                echo(f"  {table}: FAIL {rid[:8]} | {_backfill.format_error(e, 80)}", err=True)
                continue

            updates = fill_blanks(row, parsed)
            if updates:
                assignments = ", ".join(f"[{k}] = ?" for k in updates)
                with db.conn:
                    db.execute(
                        f"UPDATE [{table}] SET {assignments} WHERE id = ?",
                        [*updates.values(), rid],
                    )
                counts["details_filled"] += 1
                echo(f"  {table}: FILLED {rid[:8]} | {', '.join(sorted(updates))}")
            else:
                counts["details_unchanged"] += 1
            checkpoint[rid] = {"checked": datetime.now(timezone.utc).isoformat()}
            _save(checkpoint_path, checkpoint)

    counts["details_pending"] += max(0, len(candidates) - len(batch))
    echo(
        f"{table}: Detail backfill done: {counts['details_filled']} filled, "
        f"{counts['details_unchanged']} unchanged, {counts['details_failed']} failed, "
        f"{counts['details_pending']} still pending."
    )
    report(
        counts,
        f"details {counts['details_filled']} filled/{counts['details_failed']} failed, "
        f"{counts['details_pending']} pending",
    )
    return counts
