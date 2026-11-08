"""End-to-end fetch_data() through MockTransport, and the row shape zeeker expects.

The contract comes from zeeker.toml: each resource's ``[resource.X.columns]`` table is
the authoritative column list, and zeeker >= 0.9.0 calls fetch_data -> transform_data
(-> migrate_schema) and threads the raw fetch_data() output into fetch_fragments_data.
"""

import re
import tomllib

import enforcement_decisions as ed
import guidance_by_topic as gbt
import pytest
import regulatory_guidance as rg
from helpers import (
    DOC_CONTENT_HTML,
    FETCH_RESOURCES,
    RESOURCES_DIR,
    documented_enforcement_page,
    fixture_json,
    fixture_text,
    make_site,
)

TOML = tomllib.loads((RESOURCES_DIR.parent / "zeeker.toml").read_text(encoding="utf-8"))
ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
DOC_PDF = f"{ed.BASE_URL}/assets/11111111-2222-3333-4444-555555555555.pdf"


def _paths(listing):
    return [i["href"] for i in listing["data"]]


# --- enforcement_decisions --------------------------------------------------------


@pytest.fixture
def enforcement_run(mock_network):
    listing = fixture_json("enforcement_listing.json")
    commission, klp, vu = _paths(listing)  # listing order: Courts, KLP, DCS (VU)
    pages = {
        commission: documented_enforcement_page(DOC_CONTENT_HTML),
        vu: fixture_text("enforcement_detail_voluntary_undertaking.html"),
        # klp: 404 -> detail failure path
    }
    handler, listing = make_site("enforcement_listing.json", pages, ed)
    seen = mock_network(handler)
    records = ed.fetch_data(None)
    return records, listing, seen


def test_enforcement_fetch_data_returns_one_record_per_listing_item(enforcement_run):
    records, listing, _ = enforcement_run
    assert [r["id"] for r in records] == [i["id"] for i in listing["data"]]


def test_enforcement_record_from_documented_layout(enforcement_run):
    first = enforcement_run[0][0]
    assert first["title"].startswith("Breach of the Protection Obligation by Courts")
    assert first["organisation"] == "Courts (Singapore) Pte Ltd"
    assert first["decision_type"] == "Commission's Decisions"
    assert first["decision_date"] == "2026-03-14"  # page banner beats listing date
    assert first["decision_url"] == ed.BASE_URL + enforcement_run[1]["data"][0]["href"]
    assert first["penalty_amount"] == 12000.0
    assert first["pdf_url"] == DOC_PDF
    assert first["_pdf_text"] and len(first["_pdf_text"]) == 3100  # via docling mock
    assert ISO_TS.match(first["imported_on"])


def test_enforcement_record_when_detail_page_missing(enforcement_run):
    klp = enforcement_run[0][1]
    assert klp["summary"] == "" and klp["pdf_url"] == "" and klp["_pdf_text"] == ""
    assert klp["decision_date"] == "2026-10-26"  # falls back to the listing date
    assert klp["penalty_amount"] is None
    assert klp["organisation"] == "KLP LLP"  # multi-obligation title


def test_enforcement_does_not_call_docling_without_pdf(enforcement_run):
    _, _, seen = enforcement_run
    posts = [r for r in seen if r.method == "POST"]
    assert len(posts) == 1  # only the record that had a pdf_url


def test_enforcement_rows_match_zeeker_toml_columns(enforcement_run):
    records, _, _ = enforcement_run
    expected = set(TOML["resource"]["enforcement_decisions"]["columns"])
    for record in records:
        assert set(record) - {"_pdf_text"} == expected
    transformed = ed.transform_data([dict(r) for r in records])
    for row in transformed:
        assert set(row) == expected
        assert isinstance(row["id"], str) and isinstance(row["title"], str)
        assert isinstance(row["organisation"], str) and isinstance(row["decision_type"], str)
        assert row["decision_date"] is None or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", row["decision_date"]
        )
        assert row["decision_url"].startswith("https://www.pdpc.gov.sg/")
        assert row["penalty_amount"] is None or isinstance(row["penalty_amount"], float)
        assert isinstance(row["summary"], str) and isinstance(row["pdf_url"], str)


def test_enforcement_fragments_follow_from_the_same_run(enforcement_run):
    records, _, _ = enforcement_run
    # zeeker snapshots the raw output for the fragments phase BEFORE transform_data strips _pdf_text
    snapshot = [dict(r) for r in records]
    ed.transform_data(records)
    assert all("_pdf_text" not in r for r in records)
    frags = ed.fetch_fragments_data(None, snapshot)
    parent = records[0]["id"]
    assert [f["id"] for f in frags] == [f"{parent}_chunk_{i}" for i in range(3)]
    expected_cols = set(TOML["resource"]["enforcement_decisions_fragments"]["columns"])
    assert all(set(f) == expected_cols for f in frags)
    assert all(f["parent_id"] == parent for f in frags)


def test_enforcement_skips_known_ids_and_known_urls(mock_network, monkeypatch):
    listing = fixture_json("enforcement_listing.json")
    first, second, third = listing["data"]
    handler, _ = make_site("enforcement_listing.json", {}, ed)
    mock_network(handler)
    monkeypatch.setattr(ed, "_dedupe_existing_decisions", lambda db: None)
    monkeypatch.setattr(ed, "_run_fragment_backfill", lambda db: None)

    class Existing:
        db = None
        rows = [
            {"id": first["id"], "decision_url": ""},  # known by id
            # known by URL under a different id (PDPC republishes under a new UUID)
            {"id": "other-uuid", "decision_url": ed.BASE_URL + second["href"]},
        ]

    records = ed.fetch_data(Existing())
    assert [r["id"] for r in records] == [third["id"]]


def test_enforcement_migrate_schema_patches_inferred_types():
    migration = {"new_schema": {"penalty_amount": "TEXT", "_pdf_text": "TEXT", "id": "TEXT"}}
    assert ed.migrate_schema(None, migration) is True
    assert migration["new_schema"] == {"penalty_amount": "REAL", "id": "TEXT"}
    keep = {"new_schema": {"penalty_amount": "INTEGER"}}
    ed.migrate_schema(None, keep)
    assert keep["new_schema"]["penalty_amount"] == "INTEGER"


# --- guidance_by_topic / regulatory_guidance --------------------------------------

GUIDANCE_CASES = {
    "guidance_by_topic": (
        gbt,
        "guidance_listing.json",
        ["guidance_detail_publication.html", "guidance_detail_training.html"],
    ),
    "regulatory_guidance": (rg, "regulatory_listing.json", ["regulatory_detail_advisory.html"]),
}


@pytest.fixture(params=sorted(GUIDANCE_CASES))
def guidance_run(request, mock_network):
    mod, listing_name, detail_files = GUIDANCE_CASES[request.param]
    listing = fixture_json(listing_name)
    hrefs = _paths(listing)
    pages = {hrefs[i]: fixture_text(f) for i, f in enumerate(detail_files)}
    # One item links straight at a PDF asset (the "/assets/" shortcut branch).
    asset_item = {
        "id": "asset-item",
        "topic": "Publications",
        "title": "Direct PDF item",
        "href": "/assets/99999999-0000-0000-0000-000000000000",
        "date": "01 Jan 2026",
    }
    handler, listing = make_site(listing_name, pages, mod, extra_items=[asset_item])
    seen = mock_network(handler)
    return mod, mod.fetch_data(None), listing, seen


def test_guidance_fetch_data_records(guidance_run):
    mod, records, listing, seen = guidance_run
    assert [r["id"] for r in records] == [i["id"] for i in listing["data"]]
    by_id = {r["id"]: r for r in records}
    direct = by_id["asset-item"]
    assert direct["pdf_url"] == f"{mod.BASE_URL}/assets/99999999-0000-0000-0000-000000000000"
    assert direct["published_date"] == "2026-01-01"  # listing-date fallback
    assert len(direct["_pdf_text"]) == 3100
    first = records[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", first["published_date"])
    assert first["summary"] and first["page_url"].startswith(mod.BASE_URL)
    assert [r for r in seen if r.method == "POST"]  # docling reached for the direct PDF


def test_guidance_rows_match_zeeker_toml_columns(guidance_run):
    mod, records, _, _ = guidance_run
    expected = set(TOML["resource"][mod.RESOURCE_NAME]["columns"])
    for record in records:
        assert set(record) - {"_pdf_text"} == expected
    for row in mod.transform_data([dict(r) for r in records]):
        assert set(row) == expected
        assert all(
            isinstance(row[k], str)
            for k in ("id", "title", "topic", "page_url", "pdf_url", "summary")
        )
        assert row["published_date"] is None or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", row["published_date"]
        )
        assert ISO_TS.match(row["imported_on"])


def test_guidance_fragments_for_cta_and_direct_pdfs(guidance_run):
    """Detail pages whose banner CTA points at an asset get fragments, as does the
    item that links straight at a PDF; the training page (external CTA) gets none."""
    mod, records, _, _ = guidance_run
    with_docs = [r["id"] for r in records if r["pdf_url"]]
    assert "asset-item" in with_docs and len(with_docs) == 2
    snapshot = [dict(r) for r in records]
    frags = mod.fetch_fragments_data(None, snapshot)
    assert [f["id"] for f in frags] == [f"{d}_chunk_{i}" for d in with_docs for i in range(3)]
    frag_cols = set(TOML["resource"]["enforcement_decisions_fragments"]["columns"])
    assert all(set(f) == frag_cols == set(mod.FRAGMENT_COLUMNS) for f in frags)


def test_guidance_migrate_schema_drops_internal_field():
    for mod in (gbt, rg):
        migration = {"new_schema": {"published_date": "TEXT", "_pdf_text": "TEXT"}}
        assert mod.migrate_schema(None, migration) is True
        assert migration["new_schema"] == {"published_date": "TEXT"}


# --- zeeker.toml <-> code consistency ---------------------------------------------


@pytest.mark.parametrize("name", FETCH_RESOURCES)
def test_toml_declares_fragments_with_matching_columns(name):
    section = TOML["resource"][name]
    assert section["fragments"] is True
    module = __import__(name)
    frag_section = TOML["resource"].get(f"{name}_fragments")
    if frag_section is not None:  # enforcement_decisions documents its fragments table
        assert set(frag_section["columns"]) == set(module.FRAGMENT_COLUMNS)
    assert set(section["fts_fields"]) <= set(section["columns"])
    assert section["label_column"] in section["columns"]


def test_every_toml_resource_has_a_module():
    for name in TOML["resource"]:
        assert (RESOURCES_DIR / f"{name}.py").exists(), name
