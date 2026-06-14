"""
PDPC Enforcement Decisions resource.

Source: https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions
Requires: TAILSCALE_PROXY — CloudFront blocks data-centre IPs (403)
Requires: DOCLING_SERVE_URL — PDF text extraction via docling server

Listing API (confirmed 2026-05-06):
  GET /api/listing-api?listingtype=enforcement_decisions&itemsperpage=10
      &slug=organisations%2Fregulations-decisions%2Fenforcement-decisions
      &pathname=%2Forganisations%2Fregulations-decisions%2Fenforcement-decisions
      &page=N&sort=latest&type=All
  → {totalItems: 376, data: [{id, topic, title, image, href, date}]}

Detail pages (Next.js RSC stream):
  Row 22: "Published on DD Mon YYYY"
  Row 24: {"content": "<p>Brief summary. Click <a href='/assets/UUID'>here</a>..."}
  The brief HTML summary + a link to the full decision PDF.

PDF strategy:
  PDF bytes are fetched through the Tailscale proxy (CloudFront blocks data-centre IPs for
  asset downloads too). Bytes are then POSTed to the docling server via /v1/convert/file.
  Extracted markdown is chunked and stored in the fragments table for FTS.
  The brief HTML summary is stored on the main record but is NOT fragmented.

Fragment generation note:
  Zeeker calls fetch_data() twice per build — once to insert main records, and once to pass
  main_data_context to fetch_fragments_data(). The second call finds all records in
  existing_table and returns []. To work around this, fetch_data() caches new records
  (with PDF text) in a module-level variable before stripping the internal field.
"""

import json
import os
import re
import time
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_URL = "https://www.pdpc.gov.sg"
LISTING_API = f"{BASE_URL}/api/listing-api"
LISTING_SLUG = "organisations/regulations-decisions/enforcement-decisions"
LISTING_PATHNAME = "/" + LISTING_SLUG
ITEMS_PER_PAGE = 10

DOCLING_SERVE_URL = os.environ.get("DOCLING_SERVE_URL", "http://localhost:5001")

BACKFILL_BATCH_SIZE = int(os.environ.get("PDPC_BACKFILL_BATCH_SIZE", "10"))
BACKFILL_MAX_RETRIES = int(os.environ.get("PDPC_BACKFILL_MAX_RETRIES", "3"))
BACKFILL_CHECKPOINT = "checkpoint_pdpc_fragments.json"

REQUEST_DELAY_BASE = 1.5
REQUEST_DELAY_JITTER = 0.5
REQUEST_TIMEOUT = 30.0
PDF_TIMEOUT = 60.0
MAX_CONSECUTIVE_FAILURES = 5
MAX_RETRIES = 3

HTTP_LIMITS = httpx.Limits(max_connections=2, max_keepalive_connections=2)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Safari/605.1.15"
)

_NEXT_F_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)'
)

DB_PATH = "pdpc.db"  # relative to project root where zeeker runs

# os.environ sentinel: set to str(os.getpid()) after backfill runs once per process.
# Module-level variables are reset on each zeeker module reload, but os.environ persists.
_BACKFILL_SENTINEL_KEY = "_PDPC_BACKFILL_RAN_PID"

FRAGMENT_COLUMNS = {
    "id": str,
    "parent_id": str,
    "text": str,
    "sequence": int,
    "content_type": str,
    "char_count": int,
}


# =============================================================================
# HELPERS
# =============================================================================

def _polite_sleep():
    time.sleep(max(0.5, REQUEST_DELAY_BASE + random.uniform(
        -REQUEST_DELAY_JITTER, REQUEST_DELAY_JITTER
    )))


def _dedupe_existing_decisions(db) -> None:
    """Remove duplicate enforcement_decisions rows that share a decision_url.

    PDPC sometimes republishes a decision under a *new* listing UUID (same URL,
    same date — this happens with the same-date Voluntary Undertaking batches).
    fetch_data() skips items by ``id`` only, so the re-published row slips past
    the id check and gets appended a second time. ``decision_url`` is the natural
    key for a decision, so we keep the earliest-imported row per URL (lowest
    rowid) and drop the rest, along with any fragments orphaned by the deletion.

    Idempotent: once the DB is clean this is a no-op. Runs as a side effect of
    fetch_data() so it cleans the S3-synced DB on the next build.
    """
    tbl = db["enforcement_decisions"]
    if not tbl.exists():
        return

    dup_ids = [
        row["id"]
        for row in db.query(
            """
            SELECT id FROM enforcement_decisions
            WHERE decision_url IS NOT NULL AND decision_url != ''
              AND rowid NOT IN (
                SELECT MIN(rowid) FROM enforcement_decisions
                WHERE decision_url IS NOT NULL AND decision_url != ''
                GROUP BY decision_url
              )
            """
        )
    ]
    if not dup_ids:
        return

    placeholders = ",".join("?" * len(dup_ids))
    frags_tbl = db["enforcement_decisions_fragments"]
    with db.conn:
        tbl.delete_where(f"id in ({placeholders})", dup_ids)
        if frags_tbl.exists():
            frags_tbl.delete_where(f"parent_id in ({placeholders})", dup_ids)
    click.echo(
        f"Deduped enforcement_decisions: removed {len(dup_ids)} duplicate "
        f"row(s) sharing a decision_url."
    )


def _ensure_fragments_table(db) -> None:
    """Create enforcement_decisions_fragments if it doesn't exist."""
    import sqlite_utils as _su
    tbl = db["enforcement_decisions_fragments"]
    if not tbl.exists():
        db["enforcement_decisions_fragments"].create(
            FRAGMENT_COLUMNS, pk="id", if_not_exists=True
        )


def _run_fragment_backfill(db) -> None:
    """Fetch PDFs and insert fragments for decisions that don't have any yet.

    Runs inline from fetch_data() as a side effect so it executes even when
    fetch_data returns [] (zeeker marks the resource as 'skipped' in that case
    and never calls fetch_fragments_data). Uses os.environ sentinel to run at
    most once per process across zeeker's multiple fetch_data calls per build.
    """
    sentinel = _BACKFILL_SENTINEL_KEY
    if os.environ.get(sentinel) == str(os.getpid()):
        return  # Already ran in this process
    os.environ[sentinel] = str(os.getpid())

    _ensure_fragments_table(db)

    # Decisions that have a pdf_url but no fragments yet
    existing_parent_ids: set = set()
    frags_tbl = db["enforcement_decisions_fragments"]
    if frags_tbl.exists():
        for row in frags_tbl.rows_where(select="parent_id"):
            existing_parent_ids.add(row["parent_id"])

    all_decisions = list(db["enforcement_decisions"].rows_where(
        "pdf_url IS NOT NULL AND pdf_url != ''"
    ))

    checkpoint = _load_backfill_checkpoint()
    now_ts = datetime.now(timezone.utc).isoformat()

    candidates = [
        d for d in all_decisions
        if d["id"] not in existing_parent_ids
        and checkpoint.get(d["id"], {}).get("failures", 0) < BACKFILL_MAX_RETRIES
    ]

    total_pending = len(candidates)
    quarantined = sum(1 for v in checkpoint.values() if v.get("failures", 0) >= BACKFILL_MAX_RETRIES)

    if not candidates:
        click.echo(f"Fragment backfill: nothing to do ({quarantined} quarantined).")
        return

    batch = candidates[:BACKFILL_BATCH_SIZE]
    click.echo(
        f"Fragment backfill: {total_pending} pending, {quarantined} quarantined — "
        f"processing {len(batch)} this run."
    )

    successes = 0
    failures = 0

    existing_frag_ids: set = set()
    if frags_tbl.exists():
        for row in frags_tbl.rows_where(select="id"):
            existing_frag_ids.add(row["id"])

    with _make_client() as client:
        for decision in batch:
            did = decision["id"]
            pdf_url = decision["pdf_url"]
            title_short = decision.get("title", "")[:50]
            try:
                _polite_sleep()
                pdf_bytes = _fetch_bytes(client, pdf_url)
                pdf_text = _convert_pdf_with_docling(pdf_bytes)
                if not pdf_text or len(pdf_text) < 50:
                    raise ValueError(f"empty PDF text ({len(pdf_text)} chars)")
                chunks = _chunk_text(pdf_text, did, existing_frag_ids)
                if chunks:
                    db["enforcement_decisions_fragments"].insert_all(chunks, replace=False, ignore=True)
                    existing_frag_ids.update(c["id"] for c in chunks)
                click.echo(f"  OK  {did[:8]} | {title_short} | {len(chunks)} chunks")
                successes += 1
                if did in checkpoint:
                    del checkpoint[did]
                    _save_backfill_checkpoint(checkpoint)
            except Exception as e:
                failures += 1
                rec = checkpoint.get(did, {"failures": 0})
                rec["failures"] = rec.get("failures", 0) + 1
                rec["last_error"] = str(e)[:200]
                rec["last_attempt"] = now_ts
                checkpoint[did] = rec
                _save_backfill_checkpoint(checkpoint)
                click.echo(f"  FAIL {did[:8]} | {str(e)[:80]}", err=True)

    new_quarantined = sum(1 for v in checkpoint.values() if v.get("failures", 0) >= BACKFILL_MAX_RETRIES)
    remaining = total_pending - len(batch)
    click.echo(
        f"Fragment backfill done: {successes} OK, {failures} failed, "
        f"{new_quarantined} quarantined, {remaining} still pending."
    )


def _load_backfill_checkpoint() -> Dict[str, Any]:
    try:
        with open(BACKFILL_CHECKPOINT) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_backfill_checkpoint(data: Dict[str, Any]) -> None:
    tmp = BACKFILL_CHECKPOINT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, BACKFILL_CHECKPOINT)


def _make_client() -> httpx.Client:
    proxy = os.environ.get("TAILSCALE_PROXY") or None
    if proxy:
        click.echo(f"Routing via {proxy}")
    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": USER_AGENT},
        limits=HTTP_LIMITS,
    )


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def _fetch_text(client: httpx.Client, url: str, **kwargs) -> str:
    resp = client.get(url, **kwargs)
    resp.raise_for_status()
    return resp.text


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def _fetch_json(client: httpx.Client, url: str, **kwargs) -> Any:
    resp = client.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def _fetch_bytes(client: httpx.Client, url: str) -> bytes:
    resp = client.get(url, timeout=PDF_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _parse_date(date_str: str) -> Optional[str]:
    date_str = date_str.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})", date_str)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt
                ).date().isoformat()
            except ValueError:
                continue
    return None


def _extract_penalty(text: str) -> Optional[float]:
    """Extract SGD penalty from text like '$12,000' or 'S$12,000'."""
    m = re.search(r"S?\$\s?([\d,]+)", text.replace("\xa0", " "))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _decode_rsc_stream(html: str) -> str:
    parts: List[str] = []
    for esc in _NEXT_F_PUSH_RE.findall(html):
        try:
            parts.append(json.loads('"' + esc + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def _extract_rsc_row(rsc: str, row_id: str) -> str:
    marker = f"\n{row_id}:"
    start = rsc.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    next_row = re.search(r"\n[0-9a-f]+:", rsc[start:])
    end = start + next_row.start() if next_row else len(rsc)
    return rsc[start:end]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    parts = []
    for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "li", "td"]):
        text = el.get_text(strip=True)
        if len(text) > 10:
            parts.append(text)
    return "\n\n".join(parts)


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def _convert_pdf_with_docling(pdf_bytes: bytes) -> str:
    """Upload PDF bytes to docling server and return extracted markdown text.

    PDF bytes must be pre-fetched through the Tailscale proxy — the docling server
    on the host cannot use the container's proxy to reach pdpc.gov.sg/assets/*.
    """
    resp = httpx.post(
        f"{DOCLING_SERVE_URL}/v1/convert/file",
        files={"files": ("decision.pdf", pdf_bytes, "application/pdf")},
        data={"options": json.dumps({"to_formats": ["md"]})},
        timeout=120.0,
    )
    resp.raise_for_status()
    results = resp.json()
    doc_result = results[0] if isinstance(results, list) and results else results
    return doc_result.get("document", {}).get("md_content", "") or ""


def _title_to_organisation(title: str) -> str:
    for prefix in [
        "Voluntary Undertaking by ",
        "Breach of the Protection Obligation by ",
        "Breach of the Consent Obligation by ",
        "Breach of the Notification Obligation by ",
        "Breach of the Accountability Obligation by ",
        "Breach of ",
    ]:
        if title.startswith(prefix):
            return title[len(prefix):]
    m = re.search(r" by (.+)$", title, re.IGNORECASE)
    return m.group(1) if m else title


# =============================================================================
# LISTING API
# =============================================================================

def _fetch_listing_page(client: httpx.Client, page: int) -> Dict[str, Any]:
    params = {
        "listingtype": "enforcement_decisions",
        "itemsperpage": str(ITEMS_PER_PAGE),
        "slug": LISTING_SLUG,
        "pathname": LISTING_PATHNAME,
        "page": str(page),
        "sort": "latest",
        "type": "All",
    }
    return _fetch_json(client, LISTING_API, params=params)


# =============================================================================
# DETAIL PAGE PARSING
# =============================================================================

def _parse_detail_page(html: str) -> Dict[str, Any]:
    """Extract summary, date, PDF URL and penalty from a decision detail page.

    Returns:
      decision_date: ISO date string or None
      summary: plain text of the brief HTML blurb
      pdf_url: absolute URL of the primary linked PDF/asset, or ""
      penalty_amount: float or None (from summary text)
    """
    rsc = _decode_rsc_stream(html)

    # Date from row 22: span.page-banner__date
    pub_date = None
    row22 = _extract_rsc_row(rsc, "22")
    if row22:
        m = re.search(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})", row22)
        if m:
            pub_date = _parse_date(m.group(1))

    # Content from row 24: {"content": "<html>"}
    content_html = ""
    row24 = _extract_rsc_row(rsc, "24")
    if row24:
        m = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', row24)
        if m:
            try:
                content_html = json.loads('"' + m[1] + '"')
            except json.JSONDecodeError:
                content_html = m.group(1)

    summary = _html_to_text(content_html)
    penalty = _extract_penalty(summary[:1000]) if summary else None

    # Primary PDF/asset link — first <a href="/assets/..."> in content
    pdf_url = ""
    if content_html:
        soup = BeautifulSoup(content_html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/assets/" in href:
                pdf_url = BASE_URL + href if href.startswith("/") else href
                break

    # Fallback: scan full page HTML for PDF links
    if not pdf_url:
        soup_page = BeautifulSoup(html, "lxml")
        for a in soup_page.select("a[href$='.pdf']"):
            href = a["href"]
            pdf_url = BASE_URL + href if href.startswith("/") else href
            break

    return {
        "decision_date": pub_date,
        "summary": summary,
        "pdf_url": pdf_url,
        "penalty_amount": penalty,
    }


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def fetch_data(existing_table) -> List[Dict[str, Any]]:
    # Fragment backfill runs as a side effect here (once per process via os.environ sentinel)
    # because zeeker only calls fetch_fragments_data when fetch_data returns new records.
    # On steady-state days (no new decisions), fragments would never be processed otherwise.
    if existing_table:
        # Clean up any historical decision_url duplicates before backfilling
        # fragments, so the backfill never processes a soon-to-be-deleted row.
        _dedupe_existing_decisions(existing_table.db)
        _run_fragment_backfill(existing_table.db)

    existing_ids: set = set()
    existing_urls: set = set()
    if existing_table:
        for row in existing_table.rows:
            existing_ids.add(row["id"])
            if row.get("decision_url"):
                existing_urls.add(row["decision_url"])
        click.echo(f"Existing records: {len(existing_ids)}")

    results = []
    consecutive_failures = 0

    with _make_client() as client:
        try:
            _polite_sleep()
            first_page = _fetch_listing_page(client, 1)
        except Exception as e:
            click.echo(f"Failed to fetch listing page 1: {e}", err=True)
            return []

        total_items = first_page.get("totalItems", 0)
        total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        click.echo(f"Total decisions: {total_items} across {total_pages} pages")

        pages_cache = {1: first_page}

        for page in range(1, total_pages + 1):
            if page not in pages_cache:
                _polite_sleep()
                try:
                    pages_cache[page] = _fetch_listing_page(client, page)
                    consecutive_failures = 0
                except Exception as e:
                    click.echo(f"  Listing page {page} failed: {e}", err=True)
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        click.echo("  Too many failures — stopping.")
                        break
                    continue

            items = pages_cache[page].get("data", [])
            if not items:
                break

            new_on_page = 0
            all_known = True

            for item in items:
                item_id = item.get("id", "")
                if not item_id or item_id in existing_ids:
                    continue

                href = item.get("href", "")
                decision_url = BASE_URL + href if href.startswith("/") else href

                # Same decision re-published under a new UUID — decision_url is
                # the natural key, so treat a known URL as already captured even
                # though its id is new. Prevents the duplicate-row bug (issue #1).
                if decision_url and decision_url in existing_urls:
                    continue

                all_known = False
                new_on_page += 1
                if decision_url:
                    existing_urls.add(decision_url)
                title = item.get("title", "")
                decision_type = item.get("topic", "")

                detail = {}
                pdf_text = ""

                if decision_url:
                    _polite_sleep()
                    try:
                        detail_html = _fetch_text(client, decision_url)
                        detail = _parse_detail_page(detail_html)
                    except Exception as e:
                        click.echo(f"  Detail fetch failed for {item_id}: {e}", err=True)

                # Fetch PDF bytes through Tailscale proxy, then send to docling
                pdf_url = detail.get("pdf_url", "")
                if pdf_url:
                    _polite_sleep()
                    try:
                        pdf_bytes = _fetch_bytes(client, pdf_url)
                        pdf_text = _convert_pdf_with_docling(pdf_bytes)
                        click.echo(f"    PDF: {len(pdf_text)} chars extracted")
                    except Exception as e:
                        click.echo(f"  PDF extract failed for {item_id}: {e}", err=True)

                decision_date = (
                    detail.get("decision_date")
                    or _parse_date(item.get("date", ""))
                )
                penalty = detail.get("penalty_amount")
                if penalty is None and pdf_text:
                    penalty = _extract_penalty(pdf_text[:3000])

                record = {
                    "id": item_id,
                    "title": title,
                    "organisation": _title_to_organisation(title),
                    "decision_type": decision_type,
                    "decision_date": decision_date,
                    "decision_url": decision_url,
                    "penalty_amount": penalty,
                    "summary": detail.get("summary", ""),
                    "pdf_url": pdf_url,
                    "imported_on": datetime.now(timezone.utc).isoformat(),
                    "_pdf_text": pdf_text,
                }
                results.append(record)
                click.echo(f"  -> {decision_type[:3]} | {title[:60]} | {decision_date}")

            click.echo(f"  Page {page}: {new_on_page} new")

            if all_known and len(existing_ids) > 0:
                click.echo(f"  All items on page {page} already known — stopping.")
                break

    # Strip internal field before inserting into DB
    for r in results:
        r.pop("_pdf_text", None)

    click.echo(f"\nDone: {len(results)} new enforcement decisions.")
    return results


def migrate_schema(existing_table, migration) -> bool:
    """Keep penalty_amount as REAL even when an all-null batch causes zeeker to infer TEXT.
    Voluntary Undertakings carry no financial penalty, so batches of VUs produce all-None
    penalty_amount values. SQLite REAL and NULL are compatible — no column alteration needed.
    We patch the inferred schema in-place (migration["new_schema"] is the same object zeeker
    will pass to update_schema_tracking) to prevent TEXT from being stored as the new type.
    """
    new_schema = migration.get("new_schema", {})
    if new_schema.get("penalty_amount") == "TEXT":
        new_schema["penalty_amount"] = "REAL"
    return True


def _chunk_text(text: str, decision_id: str, existing_fragment_ids: set) -> List[Dict[str, Any]]:
    """Split PDF text into overlapping chunks for FTS."""
    chunk_size = 1200
    overlap = 150
    fragments = []
    start, seq = 0, 0
    while start < len(text):
        chunk = text[start: start + chunk_size]
        frag_id = f"{decision_id}_chunk_{seq}"
        if frag_id not in existing_fragment_ids:
            fragments.append({
                "id": frag_id,
                "parent_id": decision_id,
                "text": chunk,
                "sequence": seq,
                "content_type": "text",
                "char_count": len(chunk),
            })
        start += chunk_size - overlap
        seq += 1
    return fragments


def fetch_fragments_data(existing_fragments_table, main_data_context=None) -> List[Dict[str, Any]]:
    """Stub — fragment backfill is handled inline in fetch_data() via _run_fragment_backfill().

    zeeker only calls fetch_fragments_data when fetch_data returns new records ("success"),
    but the backfill needs to run on every build. The side-effect approach in fetch_data
    bypasses this limitation entirely.
    """
    return []
