"""Unit tests for resources/_detail_backfill.py (re-parse rows captured with blanks)."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import _detail_backfill as dbf
import guidance_by_topic as gbt
import pytest
import sqlite_utils
from helpers import fixture_text

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def test_fill_blanks_only_fills_empty_fields():
    row = {"summary": "", "pdf_url": "kept", "penalty_amount": None, "decision_date": "2026-01-01"}
    parsed = {
        "summary": "new",
        "pdf_url": "other",
        "penalty_amount": 5.0,
        "decision_date": "2026-02-02",
        "not_a_column": "x",
    }
    assert dbf.fill_blanks(row, parsed) == {"summary": "new", "penalty_amount": 5.0}
    assert dbf.fill_blanks({"summary": ""}, {"summary": ""}) == {}


@pytest.fixture
def db():
    d = sqlite_utils.Database(memory=True)
    d["t"].insert_all(
        [
            {"id": "a", "url": "https://x.test/a", "summary": "", "pdf_url": ""},
            {"id": "b", "url": "https://x.test/b", "summary": "", "pdf_url": ""},
            {"id": "c", "url": "https://x.test/c", "summary": "already", "pdf_url": ""},
            {"id": "d", "url": "", "summary": "", "pdf_url": ""},  # no page to fetch
        ],
        pk="id",
    )
    return d


def run(db, tmp_path, pages, env=None, now=NOW):
    fetched, lines, reports = [], [], []

    def fetch_text(_client, url):
        fetched.append(url)
        page = pages.get(url)
        if isinstance(page, Exception):
            raise page
        return page

    counts = dbf.refill_from_detail_pages(
        db,
        table="t",
        url_column="url",
        where="summary = ''",
        parse=lambda html: json.loads(html),
        fetch_text=fetch_text,
        make_client=lambda: nullcontext(None),
        sleep=lambda: None,
        echo=lambda msg, err=False: lines.append(msg),
        report=lambda c, note="": reports.append((c, note)),
        env=env or {},
        now=now,
    )
    return counts, fetched, reports


def test_fills_rows_and_records_state_in_the_db(db, tmp_path):
    pages = {
        "https://x.test/a": json.dumps({"summary": "A text", "pdf_url": "https://x.test/a.pdf"}),
        "https://x.test/b": json.dumps({"summary": ""}),  # parsed, still nothing
    }
    counts, fetched, reports = run(db, tmp_path, pages)
    assert fetched == ["https://x.test/a", "https://x.test/b"]  # c has a summary, d no url
    assert counts == {
        "details_filled": 1,
        "details_unchanged": 1,
        "details_failed": 0,
        "details_pending": 0,
    }
    assert db["t"].get("a")["summary"] == "A text"
    assert db["t"].get("a")["pdf_url"] == "https://x.test/a.pdf"
    assert reports and reports[-1][0] == counts

    # State lives in the DB (the only thing that survives between CI builds).
    assert {r["id"] for r in db[dbf.STATE_TABLE].rows if r["checked_at"]} == {"a", "b"}
    # Both were checked: a second run fetches nothing, b included.
    _, fetched, _ = run(db, tmp_path, pages)
    assert fetched == []
    # ...unless a recheck is forced.
    _, fetched, _ = run(db, tmp_path, pages, env={dbf.RECHECK_ENV: "1"})
    assert fetched == ["https://x.test/b"]


def test_failures_retry_until_cap_then_wait_for_ttl(db, tmp_path):
    pages = {"https://x.test/a": RuntimeError("boom"), "https://x.test/b": json.dumps({})}
    for attempt in range(1, 4):
        counts, fetched, _ = run(db, tmp_path, pages)
        assert "https://x.test/a" in fetched, attempt
        assert counts["details_failed"] == 1
    st = db[dbf.STATE_TABLE].get(("t", "a"))
    assert st["failures"] == 3 and st["last_error"].startswith("RuntimeError")
    assert st["checked_at"] is None
    assert counts["details_pending"] == 0  # quarantined, not pending

    _, fetched, _ = run(db, tmp_path, pages)
    assert fetched == []  # quarantined
    _, fetched, _ = run(db, tmp_path, pages, now=NOW + timedelta(days=30))
    assert fetched == ["https://x.test/a"]  # TTL elapsed


def test_batch_size_limits_and_reports_pending(db, tmp_path):
    pages = {u: json.dumps({"summary": "s"}) for u in ("https://x.test/a", "https://x.test/b")}
    counts, fetched, _ = run(db, tmp_path, pages, env={dbf.BATCH_SIZE_ENV: "1"})
    assert fetched == ["https://x.test/a"] and counts["details_pending"] == 1
    counts, fetched, _ = run(db, tmp_path, pages, env={dbf.BATCH_SIZE_ENV: "0"})
    assert fetched == [] and counts["details_filled"] == 0


def test_missing_table_is_a_noop(tmp_path):
    counts, fetched, _ = run(sqlite_utils.Database(memory=True), tmp_path, {})
    assert fetched == [] and counts["details_filled"] == 0


def test_guidance_backfill_fills_pdf_url_from_cta(tmp_path, monkeypatch):
    """The guidance wiring: blank pdf_url filled from the live page's banner CTA;
    direct /assets/ items are never fetched as pages."""
    monkeypatch.chdir(tmp_path)
    page_url = gbt.BASE_URL + "/organisations/resources/guidance-by-topic/guide-on-federated-learning"
    asset_url = gbt.BASE_URL + "/assets/direct"
    d = sqlite_utils.Database(memory=True)
    d[gbt.RESOURCE_NAME].insert_all(
        [
            {"id": "g1", "page_url": page_url, "pdf_url": "", "summary": "has one"},
            {"id": "g2", "page_url": asset_url, "pdf_url": "", "summary": ""},
        ],
        pk="id",
    )
    fetched = []

    def fake_fetch(_client, url):
        fetched.append(url)
        return fixture_text("guidance_detail_publication.html")

    monkeypatch.setattr(gbt, "_fetch_text", fake_fetch)
    gbt._run_detail_backfill(d)
    assert fetched == [page_url]
    row = d[gbt.RESOURCE_NAME].get("g1")
    assert row["pdf_url"] == f"{gbt.BASE_URL}/assets/bb55b0bd-7367-487d-980b-4701274bca7b"
    assert row["summary"] == "has one"  # never overwritten
    assert d[dbf.STATE_TABLE].get((gbt.RESOURCE_NAME, "g1"))["checked_at"]
