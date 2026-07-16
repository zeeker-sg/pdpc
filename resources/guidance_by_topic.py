"""
PDPC Guidance By Topic resource.

Source: https://www.pdpc.gov.sg/organisations/resources/guidance-by-topic
Requires: TAILSCALE_PROXY — CloudFront blocks data-centre IPs (403)
Requires: DOCLING_SERVE_URL — PDF text extraction via docling server (for PDF-linked guides)

Listing API (confirmed 2026-06-26):
  GET /api/listing-api?listingtype=guidance_by_topic
      &slug=organisations/resources/guidance-by-topic
      &pathname=/organisations/resources/guidance-by-topic
      &itemsperpage=10&page=N&sort=latest&type=All
  → {totalItems: 79, data: [{id, topic, title, image, href, date}]}

Detail pages (Next.js RSC stream):
  Content row varies — use the dynamic "data":{"content":"$XX"} pattern.
  Date is in row 23 (page-banner__date span).
  Title is in row 22 (h1 element).

PDF strategy:
  Some guidance pages link to PDF documents via /assets/UUID links in the content HTML.
  PDF bytes are fetched through the Tailscale proxy, then POSTed to the docling server.
  Extracted markdown is chunked into fragments for FTS.
  Pages without PDF links use the HTML content text directly as a single fragment.

Fragment generation note:
  Zeeker calls fetch_data() twice per build — once for main insert, once for fragment context.
  The second call returns [] because all records are "existing". A module-level cache
  _pending_for_fragments bridges the two calls, same pattern as enforcement_decisions.
"""

import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

# Sibling-module import shim: zeeker loads resource files via
# importlib.util.spec_from_file_location, which bypasses package imports —
# "from resources import _backfill" fails at build time. Prepend this file's
# directory so the shared _backfill module resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _backfill  # noqa: E402

# =============================================================================
# CONFIGURATION
# =============================================================================

RESOURCE_NAME = "guidance_by_topic"

BASE_URL = "https://www.pdpc.gov.sg"
LISTING_API = f"{BASE_URL}/api/listing-api"
LISTING_SLUG = "organisations/resources/guidance-by-topic"
LISTING_PATHNAME = "/" + LISTING_SLUG
ITEMS_PER_PAGE = 10

DOCLING_SERVE_URL = os.environ.get("DOCLING_SERVE_URL", "http://localhost:5001")

BACKFILL_BATCH_SIZE = int(os.environ.get("PDPC_GUIDANCE_BACKFILL_BATCH_SIZE", "10"))
BACKFILL_MAX_RETRIES = int(os.environ.get("PDPC_GUIDANCE_BACKFILL_MAX_RETRIES", "3"))
BACKFILL_CHECKPOINT = "checkpoint_guidance_fragments.json"

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

DB_PATH = "pdpc.db"

_BACKFILL_SENTINEL_KEY = "_PDPC_GUIDANCE_BACKFILL_RAN_PID"

# Module-level cache for fragments — bridges zeeker's double fetch_data() call
_pending_for_fragments: List[Dict[str, Any]] = []

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


def _ensure_fragments_table(db) -> None:
    """Create guidance_by_topic_fragments if it doesn't exist."""
    tbl = db["guidance_by_topic_fragments"]
    if not tbl.exists():
        db["guidance_by_topic_fragments"].create(
            FRAGMENT_COLUMNS, pk="id", if_not_exists=True
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_guidance_by_topic_fragments_parent_id"
        " ON guidance_by_topic_fragments(parent_id)"
    )


def _run_fragment_backfill(db) -> None:
    """Fetch PDFs and insert fragments for guidance items that don't have any yet.

    Runs inline from fetch_data() as a side effect so it executes even when
    fetch_data returns [] (zeeker marks the resource as 'skipped' in that case
    and never calls fetch_fragments_data). Uses os.environ sentinel to run at
    most once per process across zeeker's multiple fetch_data calls per build.
    """
    sentinel = _BACKFILL_SENTINEL_KEY
    if os.environ.get(sentinel) == str(os.getpid()):
        return
    os.environ[sentinel] = str(os.getpid())

    _ensure_fragments_table(db)

    existing_parent_ids: set = set()
    frags_tbl = db["guidance_by_topic_fragments"]
    if frags_tbl.exists():
        for row in frags_tbl.rows_where(select="parent_id"):
            existing_parent_ids.add(row["parent_id"])

    all_items = list(db["guidance_by_topic"].rows_where(
        "pdf_url IS NOT NULL AND pdf_url != ''"
    ))

    checkpoint = _load_backfill_checkpoint()
    now = datetime.now(timezone.utc)
    retry_after = _backfill.get_retry_after_seconds()
    retry_quarantined = _backfill.get_retry_quarantined()

    candidates = [
        d for d in all_items
        if d["id"] not in existing_parent_ids
        and _backfill.is_eligible(
            checkpoint.get(d["id"]),
            BACKFILL_MAX_RETRIES,
            now,
            retry_after,
            retry_quarantined,
        )
    ]

    total_pending = len(candidates)
    quarantined_start = sum(
        1 for v in checkpoint.values() if _backfill.is_quarantined(v, BACKFILL_MAX_RETRIES)
    )
    requeued = sum(
        1
        for d in candidates
        if _backfill.is_quarantined(checkpoint.get(d["id"]), BACKFILL_MAX_RETRIES)
    )

    if not candidates:
        click.echo(
            f"{RESOURCE_NAME}: Fragment backfill: nothing to do "
            f"({quarantined_start} quarantined total)."
        )
        return

    batch = candidates[:BACKFILL_BATCH_SIZE]
    click.echo(
        f"{RESOURCE_NAME}: Fragment backfill: {total_pending} pending "
        f"({requeued} re-eligible after quarantine TTL), "
        f"{quarantined_start} quarantined total — processing {len(batch)} this run."
    )

    successes = 0
    failures = 0
    failed_retryable = 0  # failed this batch but below the quarantine cap — will retry

    existing_frag_ids: set = set()
    if frags_tbl.exists():
        for row in frags_tbl.rows_where(select="id"):
            existing_frag_ids.add(row["id"])

    with _make_client() as client:
        for item in batch:
            iid = item["id"]
            pdf_url = item["pdf_url"]
            title_short = item.get("title", "")[:50]
            try:
                _polite_sleep()
                pdf_bytes = _fetch_bytes(client, pdf_url)
                pdf_text = _convert_pdf_with_docling(pdf_bytes)
                if not pdf_text or len(pdf_text) < _backfill.MIN_PDF_TEXT_CHARS:
                    raise ValueError(f"empty PDF text ({len(pdf_text)} chars)")
                chunks = _chunk_text(pdf_text, iid, existing_frag_ids)
                if chunks:
                    db["guidance_by_topic_fragments"].insert_all(chunks, replace=False, ignore=True)
                    existing_frag_ids.update(c["id"] for c in chunks)
                click.echo(f"  {RESOURCE_NAME}: OK  {iid[:8]} | {title_short} | {len(chunks)} chunks")
                successes += 1
                if iid in checkpoint:
                    del checkpoint[iid]
                    _save_backfill_checkpoint(checkpoint)
            except Exception as e:
                failures += 1
                error_str = _backfill.format_error(e)
                rec = checkpoint.get(iid, {"failures": 0})
                rec["failures"] = rec.get("failures", 0) + 1
                rec["last_error"] = error_str[:200]
                rec["last_attempt"] = datetime.now(timezone.utc).isoformat()
                checkpoint[iid] = rec
                _save_backfill_checkpoint(checkpoint)
                if rec["failures"] < BACKFILL_MAX_RETRIES:
                    failed_retryable += 1
                click.echo(
                    f"  {RESOURCE_NAME}: FAIL {iid[:8]} | {_backfill.format_error(e, 80)}",
                    err=True,
                )

    quarantined_end = sum(
        1 for v in checkpoint.values() if _backfill.is_quarantined(v, BACKFILL_MAX_RETRIES)
    )
    still_pending = _backfill.compute_still_pending(total_pending, len(batch), failed_retryable)
    click.echo(
        f"{RESOURCE_NAME}: Fragment backfill done: {successes} OK, {failures} failed, "
        f"{still_pending} still pending, {quarantined_end} quarantined total "
        f"({quarantined_end - quarantined_start:+d} this run)."
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
        click.echo(f"{RESOURCE_NAME}: Routing via {proxy}")
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


def _extract_content_html(rsc: str) -> str:
    """Dynamically find and extract the main page content from the RSC stream.

    Uses the "data":{"content":"$XX"} pattern to find the content row.
    Falls back to inline content if no $ref is present.
    Filters out cookie consent banner content (which has date_created key).
    """
    # Pattern 1: content as a $ref to another row
    # Look for "data":{"content":"$XX"} but NOT "data":{"date_created":...,"content":...}
    for m in re.finditer(r'"data":\{"content":"\$([0-9a-f]+)"\}', rsc):
        row_id = m.group(1)
        row = _extract_rsc_row(rsc, row_id)
        if row.startswith("T"):
            comma = row.find(",")
            if comma != -1:
                return row[comma + 1:]
        return row

    # Pattern 2: content inline as a direct HTML string
    # Filter out cookie banner by checking for date_created in the same object
    for m in re.finditer(r'"data":\{((?:[^{}]|\{[^}]*\})*)\}', rsc):
        inner = m.group(1)
        if '"content"' in inner and '"date_created"' not in inner:
            content_match = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', inner)
            if content_match:
                try:
                    return json.loads('"' + content_match.group(1) + '"')
                except json.JSONDecodeError:
                    return content_match.group(1)

    return ""


def _extract_date(rsc: str) -> Optional[str]:
    """Extract publication date from RSC row 23 (page-banner__date span)."""
    m = re.search(
        r'page-banner__date.*?"Published on\s*","(\d{1,2}\s+[A-Za-z]+\s+\d{4})"',
        rsc,
    )
    if m:
        return _parse_date(m.group(1))
    m = re.search(r"Published on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", rsc)
    return _parse_date(m.group(1)) if m else None


def _extract_title(rsc: str) -> str:
    """Extract the h1 title from RSC row 22."""
    row22 = _extract_rsc_row(rsc, "22")
    m = re.search(r'"children"\s*:\s*"([^"]+)"', row22)
    return m.group(1).strip() if m else ""


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


def _extract_asset_links(content_html: str) -> List[str]:
    """Extract all /assets/ links from content HTML, return absolute URLs."""
    if not content_html:
        return []
    soup = BeautifulSoup(content_html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/assets/" in href:
            if href.startswith("/"):
                href = BASE_URL + href
            links.append(href)
    return links


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def _convert_pdf_with_docling(pdf_bytes: bytes) -> str:
    """Upload PDF bytes to docling server and return extracted markdown text."""
    resp = httpx.post(
        f"{DOCLING_SERVE_URL}/v1/convert/file",
        files={"files": ("document.pdf", pdf_bytes, "application/pdf")},
        data={"options": json.dumps({"to_formats": ["md"]})},
        timeout=120.0,
    )
    resp.raise_for_status()
    results = resp.json()
    doc_result = results[0] if isinstance(results, list) and results else results
    return doc_result.get("document", {}).get("md_content", "") or ""


# =============================================================================
# LISTING API
# =============================================================================

def _fetch_listing_page(client: httpx.Client, page: int) -> Dict[str, Any]:
    params = {
        "listingtype": "guidance_by_topic",
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
    """Extract summary, date, PDF URL from a guidance detail page.

    Returns:
      published_date: ISO date string or None
      summary: plain text of the page content
      pdf_url: URL of the primary linked PDF/asset, or ""
    """
    rsc = _decode_rsc_stream(html)

    pub_date = _extract_date(rsc)
    content_html = _extract_content_html(rsc)
    summary = _html_to_text(content_html)

    # Primary PDF/asset link — first /assets/ link in content
    asset_links = _extract_asset_links(content_html)
    pdf_url = asset_links[0] if asset_links else ""

    # Fallback: scan full page HTML for PDF links
    if not pdf_url:
        soup_page = BeautifulSoup(html, "lxml")
        for a in soup_page.select("a[href$='.pdf']"):
            href = a["href"]
            pdf_url = BASE_URL + href if href.startswith("/") else href
            break

    return {
        "published_date": pub_date,
        "summary": summary,
        "pdf_url": pdf_url,
    }


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def fetch_data(existing_table) -> List[Dict[str, Any]]:
    global _pending_for_fragments

    # Fragment backfill runs as a side effect here (once per process via os.environ sentinel)
    if existing_table:
        _run_fragment_backfill(existing_table.db)

    existing_ids: set = set()
    if existing_table:
        existing_ids = {row["id"] for row in existing_table.rows}
        click.echo(f"{RESOURCE_NAME}: Existing records: {len(existing_ids)}")

    results = []
    consecutive_failures = 0

    with _make_client() as client:
        try:
            _polite_sleep()
            first_page = _fetch_listing_page(client, 1)
        except Exception as e:
            click.echo(
                f"{RESOURCE_NAME}: ABORTED (listing fetch failed: "
                f"{_backfill.format_error(e)}) — 0 new",
                err=True,
            )
            return []

        total_items = first_page.get("totalItems", 0)
        total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        click.echo(f"{RESOURCE_NAME}: Total guidance items: {total_items} across {total_pages} pages")

        pages_cache = {1: first_page}

        for page in range(1, total_pages + 1):
            if page not in pages_cache:
                _polite_sleep()
                try:
                    pages_cache[page] = _fetch_listing_page(client, page)
                    consecutive_failures = 0
                except Exception as e:
                    click.echo(
                        f"  {RESOURCE_NAME}: Listing page {page} failed: "
                        f"{_backfill.format_error(e)}",
                        err=True,
                    )
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        click.echo(f"  {RESOURCE_NAME}: Too many failures — stopping.", err=True)
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

                all_known = False
                new_on_page += 1
                href = item.get("href", "")
                page_url = BASE_URL + href if href.startswith("/") else href
                title = item.get("title", "")
                topic = item.get("topic", "")

                detail = {}
                pdf_text = ""

                if page_url and "/assets/" not in page_url:
                    _polite_sleep()
                    try:
                        detail_html = _fetch_text(client, page_url)
                        detail = _parse_detail_page(detail_html)
                    except Exception as e:
                        click.echo(
                            f"  {RESOURCE_NAME}: Detail fetch failed for {item_id}: "
                            f"{_backfill.format_error(e)}",
                            err=True,
                        )
                elif page_url and "/assets/" in page_url:
                    # Listing item links directly to a PDF asset
                    detail = {"published_date": None, "summary": "", "pdf_url": page_url}

                # Fetch PDF bytes through Tailscale proxy, then send to docling
                pdf_url = detail.get("pdf_url", "")
                if pdf_url:
                    _polite_sleep()
                    try:
                        pdf_bytes = _fetch_bytes(client, pdf_url)
                        pdf_text = _convert_pdf_with_docling(pdf_bytes)
                        if len(pdf_text) < _backfill.MIN_PDF_TEXT_CHARS:
                            # Same success threshold as the backfill: too short is a
                            # failed extraction — leave it for the backfill to retry.
                            click.echo(
                                f"  {RESOURCE_NAME}: PDF extract too short for {item_id} "
                                f"({len(pdf_text)} chars < {_backfill.MIN_PDF_TEXT_CHARS}) — "
                                f"leaving for backfill",
                                err=True,
                            )
                            pdf_text = ""
                        else:
                            click.echo(f"    {RESOURCE_NAME}: PDF: {len(pdf_text)} chars extracted")
                    except Exception as e:
                        click.echo(
                            f"  {RESOURCE_NAME}: PDF extract failed for {item_id}: "
                            f"{_backfill.format_error(e)}",
                            err=True,
                        )

                published_date = (
                    detail.get("published_date")
                    or _parse_date(item.get("date", ""))
                )

                record = {
                    "id": item_id,
                    "title": title,
                    "topic": topic,
                    "published_date": published_date,
                    "page_url": page_url,
                    "pdf_url": pdf_url,
                    "summary": detail.get("summary", ""),
                    "imported_on": datetime.now(timezone.utc).isoformat(),
                    "_pdf_text": pdf_text,
                }
                results.append(record)
                click.echo(f"  {RESOURCE_NAME}: -> {topic[:15]} | {title[:60]} | {published_date}")

            click.echo(f"  {RESOURCE_NAME}: Page {page}: {new_on_page} new")

            if all_known and len(existing_ids) > 0:
                click.echo(f"  {RESOURCE_NAME}: All items on page {page} already known — stopping.")
                break

    # Cache for fragments before stripping internal field
    _pending_for_fragments = []
    for r in results:
        frag_copy = dict(r)
        pdf_text = frag_copy.pop("_pdf_text", "")
        if pdf_text:
            frag_copy["_pdf_text"] = pdf_text
        _pending_for_fragments.append(frag_copy)

    # Strip internal field before inserting into DB
    for r in results:
        r.pop("_pdf_text", None)

    click.echo(f"\n{RESOURCE_NAME}: Done: {len(results)} new guidance items.")
    return results


def migrate_schema(existing_table, migration) -> bool:
    """Ensure published_date stays TEXT (consistent type across batches)."""
    return True


# =============================================================================
# FRAGMENTS
# =============================================================================

def _chunk_text(text: str, item_id: str, existing_fragment_ids: set) -> List[Dict[str, Any]]:
    """Split text into overlapping chunks for FTS."""
    chunk_size = 1200
    overlap = 150
    fragments = []
    start, seq = 0, 0
    while start < len(text):
        chunk = text[start: start + chunk_size]
        frag_id = f"{item_id}_chunk_{seq}"
        if frag_id not in existing_fragment_ids:
            fragments.append({
                "id": frag_id,
                "parent_id": item_id,
                "text": chunk,
                "sequence": seq,
                "content_type": "text",
                "char_count": len(chunk),
            })
        start += chunk_size - overlap
        seq += 1
    return fragments


def fetch_fragments_data(existing_fragments_table, main_data_context=None) -> List[Dict[str, Any]]:
    """Generate fragments from PDF text cached during fetch_data().

    Uses the module-level _pending_for_fragments cache populated by fetch_data().
    Falls back to fragment backfill side-effect for steady-state runs.
    """
    # If we have cached PDF text from the current fetch_data() call, use it
    if _pending_for_fragments:
        existing_frag_ids: set = set()
        if existing_fragments_table:
            for row in existing_fragments_table.rows_where(select="id"):
                existing_frag_ids.add(row["id"])

        all_fragments = []
        for item in _pending_for_fragments:
            pdf_text = item.get("_pdf_text", "")
            if not pdf_text or len(pdf_text) < _backfill.MIN_PDF_TEXT_CHARS:
                continue
            chunks = _chunk_text(pdf_text, item["id"], existing_frag_ids)
            all_fragments.extend(chunks)

        # Clear the cache after consuming
        _pending_for_fragments.clear()
        click.echo(f"{RESOURCE_NAME}: Fragments: {len(all_fragments)} new chunks from current batch.")
        return all_fragments

    # No new records this run — backfill handles it via side effect in fetch_data()
    return []