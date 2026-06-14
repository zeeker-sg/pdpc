"""Stub resource for the enforcement_decisions_fragments table — real rows come
from enforcement_decisions.py.

The fragments table is populated by ``_run_fragment_backfill`` running as a
side effect inside ``enforcement_decisions.fetch_data`` (zeeker.toml:
``[resource.enforcement_decisions] fragments = true``;
``fetch_fragments_data`` there is itself a stub). This file exists only
because zeeker's builder treats every ``[resource.X]`` section in zeeker.toml
as a buildable resource and fails the build when ``resources/X.py`` is
missing — and the ``[resource.enforcement_decisions_fragments]`` section must
stay, because it carries the Datasette column descriptions for the fragments
table.

Returning an empty list makes zeeker record this resource as
``[SKIP] no data returned`` without touching the table.
"""

from typing import Any, Dict, List, Optional

from sqlite_utils.db import Table


def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """No-op: fragments are written by enforcement_decisions._run_fragment_backfill."""
    return []
