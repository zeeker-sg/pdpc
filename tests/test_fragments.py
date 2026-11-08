"""Fragment-splitting tests (enforcement_decisions_fragments and its siblings).

Chunking is pure Python, but fetch_fragments_data/_run_fragment_backfill write through
sqlite-utils (a transitive dependency of zeeker), which the DB-backed tests exercise.
"""

import importlib

import enforcement_decisions as ed
import enforcement_decisions_fragments as ed_frags
import httpx
import pytest
import sqlite_utils
from helpers import FETCH_RESOURCES, fixture_json

DOC_ID = "95785727-4e5f-4f31-b62f-41b39236d1e3"


@pytest.fixture(scope="module")
def pdf_markdown():
    return fixture_json("docling_response.json")[0]["document"]["md_content"]


@pytest.fixture(params=FETCH_RESOURCES)
def mod(request):
    return importlib.import_module(request.param)


def test_chunk_text_count_order_ids_and_sizes(mod, pdf_markdown):
    chunks = mod._chunk_text(pdf_markdown, DOC_ID, set())
    assert len(pdf_markdown) == 3100
    assert [c["sequence"] for c in chunks] == [0, 1, 2]
    assert [c["id"] for c in chunks] == [f"{DOC_ID}_chunk_{i}" for i in range(3)]
    assert [c["char_count"] for c in chunks] == [1200, 1200, 1000]
    assert all(c["parent_id"] == DOC_ID and c["content_type"] == "text" for c in chunks)
    assert all(c["char_count"] == len(c["text"]) for c in chunks)


def test_chunk_text_windows_overlap_by_150(mod, pdf_markdown):
    chunks = mod._chunk_text(pdf_markdown, DOC_ID, set())
    for a, b in zip(chunks, chunks[1:]):
        assert a["text"][-150:] == b["text"][:150]
    assert chunks[0]["text"] == pdf_markdown[:1200]
    assert chunks[-1]["text"] == pdf_markdown[2100:]


def test_chunk_text_is_deterministic(mod, pdf_markdown):
    assert mod._chunk_text(pdf_markdown, DOC_ID, set()) == mod._chunk_text(
        pdf_markdown, DOC_ID, set()
    )


def test_chunk_text_skips_existing_ids_but_keeps_sequence(mod, pdf_markdown):
    chunks = mod._chunk_text(pdf_markdown, DOC_ID, {f"{DOC_ID}_chunk_1"})
    assert [c["sequence"] for c in chunks] == [0, 2]


@pytest.mark.parametrize(
    "length, expected_sizes",
    [
        (0, []),
        (1, [1]),
        (1050, [1050]),
        (1051, [1051, 1]),
        # a tail that fits entirely inside the previous chunk's overlap still becomes a chunk
        (1200, [1200, 150]),
        (1201, [1200, 151]),
        (1350, [1200, 300]),
        (2250, [1200, 1200, 150]),
    ],
)
def test_chunk_text_boundaries(mod, length, expected_sizes):
    chunks = mod._chunk_text("x" * length, "id", set())
    assert [c["char_count"] for c in chunks] == expected_sizes


def test_fragment_columns_match_chunk_keys(mod, pdf_markdown):
    chunk = mod._chunk_text(pdf_markdown, DOC_ID, set())[0]
    assert set(chunk) == set(mod.FRAGMENT_COLUMNS)
    for key, typ in mod.FRAGMENT_COLUMNS.items():
        assert isinstance(chunk[key], typ)


# --- fetch_fragments_data (zeeker >= 0.9.0 main_data_context contract) -----------


def test_fetch_fragments_data_chunks_context_records(mod, pdf_markdown):
    context = [
        {"id": "a", "_pdf_text": pdf_markdown},
        {"id": "short", "_pdf_text": "x" * 49},  # below MIN_PDF_TEXT_CHARS
        {"id": "empty", "_pdf_text": ""},
        {"id": "missing"},
        {"id": "b", "_pdf_text": "y" * 60},
    ]
    frags = mod.fetch_fragments_data(None, context)
    assert [f["id"] for f in frags] == ["a_chunk_0", "a_chunk_1", "a_chunk_2", "b_chunk_0"]


@pytest.mark.parametrize("context", [None, []])
def test_fetch_fragments_data_without_context_is_empty(mod, context):
    assert mod.fetch_fragments_data(None, context) == []


def test_fetch_fragments_data_skips_ids_already_in_table(mod, pdf_markdown):
    db = sqlite_utils.Database(memory=True)
    db["frags"].insert_all(
        [
            {
                "id": "a_chunk_0",
                "parent_id": "a",
                "text": "t",
                "sequence": 0,
                "content_type": "text",
                "char_count": 1,
            }
        ],
        pk="id",
    )
    frags = mod.fetch_fragments_data(db["frags"], [{"id": "a", "_pdf_text": pdf_markdown}])
    assert [f["id"] for f in frags] == ["a_chunk_1", "a_chunk_2"]


def test_fragments_stub_resource_returns_nothing():
    assert ed_frags.fetch_data(None) == []


# --- backfill side effect (sqlite-utils + httpx + tenacity together) -------------


def test_run_fragment_backfill_writes_fragments(tmp_path, monkeypatch, mock_network, pdf_markdown):
    monkeypatch.chdir(tmp_path)  # checkpoint file lands in tmp
    db = sqlite_utils.Database(tmp_path / "t.db")
    db["enforcement_decisions"].insert_all(
        [
            {"id": "d1", "title": "T1", "pdf_url": "https://www.pdpc.gov.sg/assets/one.pdf"},
            {"id": "d2", "title": "T2", "pdf_url": ""},  # no document: not a candidate
        ],
        pk="id",
    )

    def handler(request):
        if request.url.host == "www.pdpc.gov.sg":
            return httpx.Response(200, content=b"%PDF")
        return httpx.Response(200, json=fixture_json("docling_response.json"))

    seen = mock_network(handler)
    ed._run_fragment_backfill(db)
    rows = list(db["enforcement_decisions_fragments"].rows_where(order_by="sequence"))
    assert [r["id"] for r in rows] == ["d1_chunk_0", "d1_chunk_1", "d1_chunk_2"]
    assert {r["parent_id"] for r in rows} == {"d1"}
    assert [r.url.host for r in seen] == ["www.pdpc.gov.sg", httpx.URL(ed.DOCLING_SERVE_URL).host]
    # Second run: d1 now has fragments, nothing left to do and no more requests.
    ed._run_fragment_backfill(db)
    assert len(seen) == 2


def test_run_fragment_backfill_records_failure_in_checkpoint(tmp_path, monkeypatch, mock_network):
    monkeypatch.chdir(tmp_path)
    db = sqlite_utils.Database(tmp_path / "t.db")
    db["enforcement_decisions"].insert(
        {"id": "d1", "title": "T1", "pdf_url": "https://www.pdpc.gov.sg/a.pdf"}, pk="id"
    )
    mock_network(lambda r: httpx.Response(500))
    ed._run_fragment_backfill(db)
    checkpoint = ed._load_backfill_checkpoint()
    assert checkpoint["d1"]["failures"] == 1
    assert checkpoint["d1"]["last_error"].startswith("RetryError")
    assert db["enforcement_decisions_fragments"].count == 0
