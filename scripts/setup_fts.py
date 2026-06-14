"""Idempotent full-text search (FTS) setup for pdpc.db.

Why this exists (issue #3): the FTS field configuration already lives in
zeeker.toml (``fts_fields`` / ``fragments_fts_fields``), but the build only set
up FTS when a manual ``--setup-fts`` toggle was passed, so the PDPC tables were
never registered in the FTS index and the MCP ``search`` tool silently skipped
them. We cannot simply always pass ``--setup-fts`` either: zeeker calls
sqlite-utils' ``enable_fts`` without ``replace=True``, which raises
``table <x>_fts already exists`` on every incremental build after the first.

This script reads the same field lists from zeeker.toml (single source of
truth) and (re)builds the FTS5 indexes with ``replace=True`` plus triggers, so
it is safe to run on every build — fresh rebuild or S3-synced incremental.
"""

import sys
import tomllib
from pathlib import Path

import sqlite_utils

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "zeeker.toml"


def _enable(db: sqlite_utils.Database, table: str, fields: list[str]) -> None:
    if not db[table].exists():
        print(f"  skip {table}: table does not exist yet")
        return
    columns = {col.name for col in db[table].columns}
    valid = [f for f in fields if f in columns]
    if not valid:
        print(f"  skip {table}: none of {fields} present in table")
        return
    # replace=True makes this idempotent across every build (drops/recreates the
    # FTS table); create_triggers keeps the index current on later row inserts.
    db[table].enable_fts(valid, create_triggers=True, replace=True)
    db[table].populate_fts(valid)
    print(f"  FTS ready on {table}({', '.join(valid)}) — {db[table].count:,} rows")


def main() -> int:
    config = tomllib.loads(CONFIG.read_text())
    db_path = ROOT / config["project"]["database"]
    if not db_path.exists():
        print(f"Database {db_path} not found — run `zeeker build` first.", file=sys.stderr)
        return 1

    db = sqlite_utils.Database(str(db_path))
    print(f"Setting up FTS on {db_path.name}")

    for name, rconf in config.get("resource", {}).items():
        fts_fields = rconf.get("fts_fields")
        if fts_fields:
            _enable(db, name, fts_fields)
        if rconf.get("fragments"):
            _enable(db, f"{name}_fragments", rconf.get("fragments_fts_fields", ["text"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
