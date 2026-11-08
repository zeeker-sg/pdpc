"""Run the real ``zeeker build`` (in-process) against a mocked pdpc.gov.sg.

This is the closest offline stand-in for the weekly sync workflows: zeeker loads the
resource modules itself, calls fetch_data / transform_data / fetch_fragments_data,
infers schemas (sqlite-utils), applies migrate_schema and builds FTS tables. It
exercises zeeker, sqlite-utils, click, httpx, tenacity, bs4 and lxml together.
"""

import json
import shutil
import sqlite3

import enforcement_decisions as ed
import guidance_by_topic as gbt
import pytest
import regulatory_guidance as rg
from click.testing import CliRunner
from helpers import (
    DOC_CONTENT_HTML,
    RESOURCES_DIR,
    documented_enforcement_page,
    fixture_json,
    fixture_text,
    make_site,
    rsc_page,
)
from zeeker.cli import cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A scratch zeeker project: real resources/ and zeeker.toml, empty DB."""
    shutil.copytree(
        RESOURCES_DIR, tmp_path / "resources", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(RESOURCES_DIR.parent / "zeeker.toml", tmp_path / "zeeker.toml")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def build(*args):
    result = CliRunner().invoke(cli, ["build", *args])
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result


def rows(db_path, sql, params=()):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def columns(db_path, table):
    return [r["name"] for r in rows(db_path, f"PRAGMA table_info({table})")]


def _enforcement_site(mock_network):
    listing = fixture_json("enforcement_listing.json")
    # listing order: Courts, KLP, DCS (VU); KLP's page 404s on the first build
    commission, _klp, vu = (i["href"] for i in listing["data"])
    pages = {
        commission: documented_enforcement_page(DOC_CONTENT_HTML),
        vu: fixture_text("enforcement_detail_voluntary_undertaking.html"),
    }
    handler, listing = make_site("enforcement_listing.json", pages, ed)
    return mock_network(handler), listing, pages


def test_build_enforcement_decisions_end_to_end(project, mock_network):
    seen, listing, _pages = _enforcement_site(mock_network)
    result = build("enforcement_decisions", "--setup-fts")
    assert "[OK]" in result.output or "succeeded" in result.output

    db = project / "pdpc.db"
    decisions = rows(db, "SELECT * FROM enforcement_decisions ORDER BY id")
    assert sorted(r["id"] for r in decisions) == sorted(i["id"] for i in listing["data"])
    assert "_pdf_text" not in columns(db, "enforcement_decisions")
    assert set(columns(db, "enforcement_decisions")) >= {
        "id",
        "title",
        "organisation",
        "decision_type",
        "decision_date",
        "decision_url",
        "penalty_amount",
        "summary",
        "pdf_url",
        "imported_on",
    }
    penalties = {r["id"]: r["penalty_amount"] for r in decisions}
    assert penalties[listing["data"][0]["id"]] == 12000.0
    # penalty_amount must stay numeric even though two of three rows have no penalty
    decl_type = {r["name"]: r["type"] for r in rows(db, "PRAGMA table_info(enforcement_decisions)")}
    assert decl_type["penalty_amount"] in {"REAL", "FLOAT"}

    parent = listing["data"][0]["id"]
    frags = rows(db, "SELECT * FROM enforcement_decisions_fragments ORDER BY sequence")
    assert [f["id"] for f in frags] == [f"{parent}_chunk_{i}" for i in range(3)]
    assert [f["char_count"] for f in frags] == [1200, 1200, 1000]

    tables = {r["name"] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"enforcement_decisions_fts", "enforcement_decisions_fragments_fts"} <= tables
    hit = rows(
        db,
        "SELECT rowid FROM enforcement_decisions_fragments_fts WHERE enforcement_decisions_fragments_fts MATCH 'penalty'",
    )
    assert hit  # synthetic docling text mentions a financial penalty


def test_second_build_is_incremental_and_dedupes(project, mock_network):
    seen, listing, _pages = _enforcement_site(mock_network)
    build("enforcement_decisions")
    db = project / "pdpc.db"
    before = rows(db, "SELECT COUNT(*) AS n FROM enforcement_decisions")[0]["n"]
    n_requests = len(seen)

    # PDPC republishes a decision under a new UUID: same decision_url, new id.
    con = sqlite3.connect(db)
    original = con.execute("SELECT * FROM enforcement_decisions LIMIT 1").fetchone()
    cols = [c[1] for c in con.execute("PRAGMA table_info(enforcement_decisions)")]
    clone = dict(zip(cols, original))
    clone["id"] = "republished-uuid"
    con.execute(
        f"INSERT INTO enforcement_decisions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [clone[c] for c in cols],
    )
    con.commit()
    con.close()

    build("enforcement_decisions")
    after = rows(db, "SELECT id FROM enforcement_decisions")
    assert len(after) == before
    assert "republished-uuid" not in {r["id"] for r in after}
    # Second run: listing page, plus the detail backfill re-trying the one page that
    # gave no summary (KLP, still 404). No new-item detail fetches.
    new_requests = seen[n_requests:]
    klp_href = listing["data"][1]["href"]
    assert {
        r.url.path
        for r in new_requests
        if r.url.path != "/api/listing-api" and not r.url.path.startswith("/assets/")
    } == {klp_href}


def test_build_backfills_rows_captured_with_blank_fields(project, mock_network):
    """Rows stored by an older parser get their blanks filled from the live page,
    mangled organisations re-derived, and the recovered PDF fragmented in the same build."""
    seen, listing, pages = _enforcement_site(mock_network)
    build("enforcement_decisions", "--setup-fts")
    db = project / "pdpc.db"
    klp = listing["data"][1]
    (row,) = rows(db, "SELECT * FROM enforcement_decisions WHERE id = ?", [klp["id"]])
    assert row["summary"] == "" and row["pdf_url"] == ""

    # Simulate the old title heuristic's damage on a row the new build won't re-insert.
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE enforcement_decisions SET organisation = ? WHERE id = ?",
        ["the Protection and Accountability Obligations by KLP LLP", klp["id"]],
    )
    con.commit()
    con.close()

    # KLP's page comes back (live layout: rows 33/35, a meta row first).
    pages[klp["href"]] = rsc_page(
        {
            "15": '[["$","meta","1",{"name":"viewport","content":"width=device-width"}]]',
            "33": '["$","span",null,{"className":"page-banner__date","children":["Published on ","26 Oct 2026"]}]',
            "35": json.dumps(
                {
                    "data": {
                        "content": "<p>A financial penalty of $17,500 was imposed on KLP LLP. "
                        'Click <a href="/assets/klp-decision.pdf">here</a> for more.</p>'
                    }
                }
            ),
        }
    )
    build("enforcement_decisions", "--setup-fts")
    (row,) = rows(db, "SELECT * FROM enforcement_decisions WHERE id = ?", [klp["id"]])
    assert row["summary"].startswith("A financial penalty of $17,500 was imposed on KLP LLP")
    assert row["pdf_url"] == f"{ed.BASE_URL}/assets/klp-decision.pdf"
    assert row["penalty_amount"] == 17500.0
    assert row["organisation"] == "KLP LLP"
    assert rows(
        db, "SELECT id FROM enforcement_decisions_fragments WHERE parent_id = ?", [klp["id"]]
    ), "recovered pdf_url should be fragmented in the same build"
    hit = rows(
        db,
        "SELECT rowid FROM enforcement_decisions_fts WHERE enforcement_decisions_fts MATCH 'KLP'",
    )
    assert hit, "in-place summary update must reach the FTS index"

    # Third build: the row is filled, so its page is not fetched again.
    n = len(seen)
    build("enforcement_decisions", "--setup-fts")
    assert klp["href"] not in {r.url.path for r in seen[n:]}


@pytest.mark.parametrize(
    "mod, listing_name, detail_files",
    [
        (
            gbt,
            "guidance_listing.json",
            ["guidance_detail_publication.html", "guidance_detail_training.html"],
        ),
        (rg, "regulatory_listing.json", ["regulatory_detail_advisory.html"]),
    ],
    ids=["guidance_by_topic", "regulatory_guidance"],
)
def test_build_guidance_resources_end_to_end(
    project, mock_network, mod, listing_name, detail_files
):
    listing = fixture_json(listing_name)
    hrefs = [i["href"] for i in listing["data"]]
    pages = {hrefs[i]: fixture_text(f) for i, f in enumerate(detail_files)}
    handler, listing = make_site(listing_name, pages, mod)
    mock_network(handler)

    build(mod.RESOURCE_NAME, "--setup-fts")

    db = project / "pdpc.db"
    table = mod.RESOURCE_NAME
    got = rows(db, f"SELECT * FROM {table}")
    assert {r["id"] for r in got} == {i["id"] for i in listing["data"]}
    assert "_pdf_text" not in columns(db, table)
    assert set(columns(db, table)) >= {
        "id",
        "title",
        "topic",
        "published_date",
        "page_url",
        "pdf_url",
        "summary",
        "imported_on",
    }
    dated = [r for r in got if r["published_date"]]
    assert dated and all(len(r["published_date"]) == 10 for r in dated)
    names = {r["name"] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert f"{table}_fts" in names
