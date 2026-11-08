"""Parsing tests for guidance_by_topic and regulatory_guidance.

The two modules are near-identical (different slug, topic list, batch env vars), so
every test runs against both. Dependencies exercised: beautifulsoup4 + lxml, the
RSC decoder, regex helpers.
"""

import importlib
import json
import re

import pytest
from helpers import fixture_json, fixture_text, rsc_page

CASES = {
    "guidance_by_topic": {
        "listing": "guidance_listing.json",
        "details": ["guidance_detail_publication.html", "guidance_detail_training.html"],
        "dates": {
            "guidance_detail_publication.html": "2026-07-20",
            "guidance_detail_training.html": "2026-09-01",
        },
        "slug": "organisations/resources/guidance-by-topic",
        "listingtype": "guidance_by_topic",
    },
    "regulatory_guidance": {
        "listing": "regulatory_listing.json",
        "details": ["regulatory_detail_advisory.html"],
        "dates": {"regulatory_detail_advisory.html": "2026-07-20"},
        "slug": "organisations/regulations-decisions/regulatory-guidance",
        "listingtype": "regulatory_guidance",
    },
}

DETAIL_PARAMS = [
    pytest.param(mod, name, id=f"{mod}-{name}")
    for mod, case in CASES.items()
    for name in case["details"]
]


@pytest.fixture(params=sorted(CASES))
def mod(request):
    return importlib.import_module(request.param)


def test_listing_fixture_shape(mod):
    case = CASES[mod.RESOURCE_NAME]
    listing = fixture_json(case["listing"])
    assert isinstance(listing["totalItems"], int)
    assert len(listing["data"]) == 3
    for item in listing["data"]:
        assert {"id", "topic", "title", "href", "date"} <= set(item)
        assert item["href"].startswith("/" + case["slug"] + "/")
        assert mod._parse_date(item["date"]) is not None


def test_listing_request_parameters(mod, mock_network):
    """The query the module sends to the listing API (httpx params encoding)."""
    case = CASES[mod.RESOURCE_NAME]
    import httpx

    def handler(request):
        return httpx.Response(200, json=fixture_json(case["listing"]))

    seen = mock_network(handler)
    with mod._make_client() as client:
        page = mod._fetch_listing_page(client, 2)
    assert page["data"]
    q = dict(seen[0].url.params)
    assert seen[0].url.path == "/api/listing-api"
    assert q["listingtype"] == case["listingtype"]
    assert q["slug"] == case["slug"]
    assert q["pathname"] == "/" + case["slug"]
    assert q["page"] == "2" and q["itemsperpage"] == "10"
    assert seen[0].headers["user-agent"] == mod.USER_AGENT


@pytest.mark.parametrize("module_name, name", DETAIL_PARAMS)
def test_parse_detail_page_live_fixtures(module_name, name):
    mod = importlib.import_module(module_name)
    detail = mod._parse_detail_page(fixture_text(name))
    assert set(detail) == {"published_date", "summary", "pdf_url"}
    assert detail["published_date"] == CASES[module_name]["dates"][name]
    assert len(detail["summary"]) > 100
    assert "<" not in detail["summary"]  # tags flattened to text
    assert isinstance(detail["pdf_url"], str)


@pytest.mark.parametrize("module_name, name", DETAIL_PARAMS)
def test_content_extraction_ignores_cookie_banner(module_name, name):
    mod = importlib.import_module(module_name)
    rsc = mod._decode_rsc_stream(fixture_text(name))
    assert "date_created" in rsc  # the trap row is really present in the fixture
    content = mod._extract_content_html(rsc)
    assert content and "To offer you a" not in content


def test_content_via_row_reference_uses_text_row():
    """Pattern 1: "data":{"content":"$39"} points at a ``T<len>,<html>`` row."""
    import guidance_by_topic as g

    html_body = "<p>Referenced content long enough to keep.</p>"
    rsc = g._decode_rsc_stream(
        rsc_page(
            {
                "30": '["$","$L1",null,{"data":{"content":"$39"}}]',
                "39": f"T{len(html_body):x},{html_body}",
            }
        )
    )
    assert g._extract_content_html(rsc).strip() == html_body


def test_live_training_page_uses_row_reference():
    import guidance_by_topic as g

    rsc = g._decode_rsc_stream(fixture_text("guidance_detail_training.html"))
    assert re.search(r'"data":\{"content":"\$[0-9a-f]+"\}', rsc)
    assert g._extract_content_html(rsc).lstrip().startswith("<")


def test_extract_date_variants(mod):
    span = '"page-banner__date","children":["Published on ","3 Feb 2026"]'
    assert mod._extract_date(span) == "2026-02-03"
    assert mod._extract_date("Published on 03 February 2026") == "2026-02-03"
    assert mod._extract_date("nothing") is None


def test_extract_asset_links_on_live_related_references():
    """Real <a href="/assets/..."> markup from the publication page, via bs4/lxml."""
    import guidance_by_topic as g

    rsc = g._decode_rsc_stream(fixture_text("guidance_detail_publication.html"))
    m = re.search(r'"content":"(<h3><strong>Related References.*?)","id"', rsc)
    assert m, "Related References block missing from fixture"
    links = g._extract_asset_links(json.loads('"' + m.group(1) + '"'))
    assert links and links[0].startswith(g.BASE_URL + "/assets/")


def test_extract_asset_links_synthetic(mod):
    html = '<p><a href="/assets/aaa">one</a> <a href="https://x.test/assets/bbb">two</a> <a href="/other">no</a></p>'
    assert mod._extract_asset_links(html) == [
        f"{mod.BASE_URL}/assets/aaa",
        "https://x.test/assets/bbb",
    ]
    assert mod._extract_asset_links("") == []


def test_parse_detail_page_picks_first_asset_as_pdf(mod):
    content = '<p>Download the guide <a href="/assets/first-doc">here</a> or <a href="/assets/second">annex</a>.</p>'
    row = json.dumps({"content": content})
    html = rsc_page({"30": f'["$","$L1",null,{{"data":{row}}}]'})
    detail = mod._parse_detail_page(html)
    assert detail["pdf_url"] == f"{mod.BASE_URL}/assets/first-doc"
    assert detail["summary"].replace(" ", "").startswith("Downloadtheguide")


def test_parse_detail_page_garbage_degrades_gracefully(mod):
    assert mod._parse_detail_page("<html></html>") == {
        "published_date": None,
        "summary": "",
        "pdf_url": "",
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REAL BUG (site drift): on live pages the primary document is a banner component "
        '({"cta":{"type":"external","url":"/assets/<uuid>"}}) rather than an <a href> in '
        "the content HTML, so pdf_url is always empty and no PDF text/fragments are fetched. "
        "Remove this marker when the parser is fixed."
    ),
)
def test_parse_detail_page_finds_cta_document_on_live_page():
    import guidance_by_topic as g

    detail = g._parse_detail_page(fixture_text("guidance_detail_publication.html"))
    assert detail["pdf_url"] == f"{g.BASE_URL}/assets/bb55b0bd-7367-487d-980b-4701274bca7b"
