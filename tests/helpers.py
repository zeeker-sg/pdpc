"""Shared helpers for the offline test-suite (plain module, imported by tests)."""

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources"

# The three resources that fetch from PDPC and ship rows to zeeker.
FETCH_RESOURCES = ["enforcement_decisions", "guidance_by_topic", "regulatory_guidance"]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


def rsc_page(rows: dict) -> str:
    """Build a Next.js-style HTML page whose RSC stream carries the given rows.

    ``rows`` maps RSC row id (hex string) to the row body. The stream is emitted
    as a single ``self.__next_f.push([1, "..."])`` call, which is exactly the shape
    ``_decode_rsc_stream`` has to unpick on the real site.
    """
    stream = "".join(f"\n{rid}:{body}" for rid, body in rows.items()) + "\n"
    return (
        "<!DOCTYPE html><html><head><title>synthetic</title></head><body>"
        f"<script>self.__next_f.push([1,{json.dumps(stream)}])</script></body></html>"
    )


def documented_enforcement_page(content_html: str, published: str = "14 Mar 2026") -> str:
    """An enforcement detail page in the layout documented in the module docstring.

    Row 22 holds the ``Published on`` banner span, row 24 the ``{"content": ...}``
    blurb. The live site no longer serves this layout (see
    test_enforcement_decisions_parsing.py), so this is the only way to cover the
    parser's positive path.
    """
    return rsc_page(
        {
            "22": json.dumps(
                [
                    "$",
                    "span",
                    None,
                    {
                        "className": "page-banner__date",
                        "children": ["Published on ", published],
                    },
                ]
            ),
            "24": json.dumps({"content": content_html}),
        }
    )


DOC_CONTENT_HTML = (
    "<p>The Commission found that Example Pte Ltd failed to make reasonable security "
    "arrangements and directed it to pay a financial penalty of S$12,000.</p>"
    "<p>Short.</p>"
    "<ul><li>Second figure of $3,000 is ignored for the penalty.</li></ul>"
    '<p>Click <a href="/assets/11111111-2222-3333-4444-555555555555.pdf">here</a> '
    "for the full decision.</p>"
)


def make_site(listing_name, detail_pages, mod, *, extra_items=()):
    """Return (handler, listing). detail_pages maps href -> html (missing => 404)."""
    listing = fixture_json(listing_name)
    listing["data"] = list(listing["data"]) + list(extra_items)
    listing["totalItems"] = len(listing["data"])  # one page only
    docling = fixture_json("docling_response.json")

    def handler(request):
        path = request.url.path
        if path == "/api/listing-api":
            assert request.url.params["page"] == "1"
            return httpx.Response(200, json=listing)
        if request.method == "POST":
            assert request.url == httpx.URL(f"{mod.DOCLING_SERVE_URL}/v1/convert/file")
            return httpx.Response(200, json=docling)
        if path.startswith("/assets/"):
            return httpx.Response(200, content=b"%PDF-1.7 fake")
        if path in detail_pages:
            return httpx.Response(200, text=detail_pages[path])
        return httpx.Response(404, text="not found")

    return handler, listing
