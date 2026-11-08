"""Parsing tests for resources/enforcement_decisions.py.

Dependencies exercised: beautifulsoup4 + lxml (HTML flattening, link lookup), the
stdlib json/re RSC decoder, and the shape of the listing API payload.
"""

import json
import re

import enforcement_decisions as ed
import pytest
from helpers import (
    DOC_CONTENT_HTML,
    documented_enforcement_page,
    fixture_json,
    fixture_text,
    rsc_page,
)

LIVE_PAGES = [
    "enforcement_detail_commission.html",
    "enforcement_detail_voluntary_undertaking.html",
]


# --- listing payload -------------------------------------------------------------


def test_listing_fixture_shape():
    listing = fixture_json("enforcement_listing.json")
    assert isinstance(listing["totalItems"], int)
    assert len(listing["data"]) == 3
    for item in listing["data"]:
        assert {"id", "topic", "title", "href", "date"} <= set(item)
        assert item["href"].startswith(
            "/organisations/regulations-decisions/enforcement-decisions/"
        )
        assert ed._parse_date(item["date"]) is not None
    assert {i["topic"] for i in listing["data"]} <= {
        "Commission's Decisions",
        "Voluntary Undertakings",
    }


# --- RSC decoding ----------------------------------------------------------------


def test_decode_rsc_stream_roundtrip_and_rows():
    html = rsc_page({"22": '"hello \\"world\\" \u2019"', "24": '{"a":1}', "2a": "x"})
    rsc = ed._decode_rsc_stream(html)
    assert ed._extract_rsc_row(rsc, "22") == '"hello \\"world\\" \u2019"'
    assert ed._extract_rsc_row(rsc, "24") == '{"a":1}'
    assert ed._extract_rsc_row(rsc, "2a").strip() == "x"  # last row keeps the trailing newline
    assert ed._extract_rsc_row(rsc, "99") == ""


def test_decode_rsc_stream_concatenates_chunks_and_skips_garbage():
    html = (
        '<script>self.__next_f.push([1,"\\n22:abc"])</script>'
        '<script>self.__next_f.push([1,"def\\n24:ghi"])</script>'
        '<script>self.__next_f.push([1,"bad \\q escape"])</script>'
        "<script>self.__next_f.push([0])</script>"
    )
    rsc = ed._decode_rsc_stream(html)
    assert ed._extract_rsc_row(rsc, "22") == "abcdef"
    assert ed._extract_rsc_row(rsc, "24") == "ghi"


# --- small helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("26 Oct 2026", "2026-10-26"),
        ("26 October 2026", "2026-10-26"),
        ("2026-10-26", "2026-10-26"),
        (" 5 Sep 2025 ", "2025-09-05"),
        ("Published on 14 Mar 2026", "2026-03-14"),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert ed._parse_date(raw) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a financial penalty of S$12,000 was imposed", 12000.0),
        ("fined $3,500.", 3500.0),
        ("S$ 5,000", 5000.0),
        ("S$1,000 then $9,000", 1000.0),
        ("a\xa0penalty of S$7,250", 7250.0),
        ("no money here", None),
        ("$, nothing numeric", None),
    ],
)
def test_extract_penalty(text, expected):
    assert ed._extract_penalty(text) == expected


@pytest.mark.parametrize(
    "title, organisation",
    [
        ("Voluntary Undertaking by DCS Card Centre Pte Ltd", "DCS Card Centre Pte Ltd"),
        (
            "Breach of the Protection Obligation by Courts (Singapore) Pte Ltd",
            "Courts (Singapore) Pte Ltd",
        ),
        ("Breach of the Consent Obligation by Foo", "Foo"),
        ("Decision with no marker", "Decision with no marker"),
    ],
)
def test_title_to_organisation(title, organisation):
    assert ed._title_to_organisation(title) == organisation


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REAL BUG (heuristic): the catch-all 'Breach of ' prefix swallows titles such as "
        "'Breach of the Protection and Accountability Obligations by KLP LLP' (a real listing "
        "item) and returns 'the Protection and Accountability Obligations by KLP LLP' instead "
        "of the organisation. Remove this marker when the prefix list is fixed."
    ),
)
def test_title_to_organisation_multi_obligation_title():
    title = "Breach of the Protection and Accountability Obligations by KLP LLP"
    assert ed._title_to_organisation(title) == "KLP LLP"


def test_html_to_text_flattens_and_filters_short_elements():
    text = ed._html_to_text(DOC_CONTENT_HTML)
    parts = text.split("\n\n")
    assert parts[0].startswith("The Commission found that Example Pte Ltd")
    assert "Short." not in parts  # <= 10 characters are dropped
    assert any(p.startswith("Second figure of $3,000") for p in parts)
    # get_text(strip=True) glues text around inline tags ("Clickherefor ..."): compare
    # without whitespace so the test holds if that quirk is ever fixed.
    assert parts[-1].replace(" ", "") == "Clickhereforthefulldecision."
    assert ed._html_to_text("") == ""
    assert ed._html_to_text("<div>   </div>") == ""


# --- detail page: documented layout (rows 22 / 24) -------------------------------


def test_parse_detail_page_documented_layout():
    detail = ed._parse_detail_page(documented_enforcement_page(DOC_CONTENT_HTML))
    assert detail["decision_date"] == "2026-03-14"
    assert detail["penalty_amount"] == 12000.0
    assert detail["pdf_url"] == f"{ed.BASE_URL}/assets/11111111-2222-3333-4444-555555555555.pdf"
    assert detail["summary"].startswith("The Commission found that Example Pte Ltd")
    assert set(detail) == {"decision_date", "summary", "pdf_url", "penalty_amount"}


def test_parse_detail_page_absolute_asset_link_kept_as_is():
    html = documented_enforcement_page(
        '<p>Read the decision <a href="https://x.test/assets/abc">here</a> now.</p>'
    )
    assert ed._parse_detail_page(html)["pdf_url"] == "https://x.test/assets/abc"


def test_parse_detail_page_without_content_degrades_to_empty():
    detail = ed._parse_detail_page("<html><body>nothing</body></html>")
    assert detail == {"decision_date": None, "summary": "", "pdf_url": "", "penalty_amount": None}


# --- detail page: real captured pages --------------------------------------------


@pytest.mark.parametrize("name", LIVE_PAGES)
def test_parse_detail_page_live_fixture_is_well_formed(name):
    """Whatever the parser finds on real pages must have the right types."""
    detail = ed._parse_detail_page(fixture_text(name))
    assert isinstance(detail["summary"], str)
    assert isinstance(detail["pdf_url"], str)
    assert detail["decision_date"] is None or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", detail["decision_date"]
    )
    assert detail["penalty_amount"] is None or isinstance(detail["penalty_amount"], float)


def _live_content_html(name: str) -> str:
    """Lift the content blurb out of a captured page independently of the parser."""
    rsc = ed._decode_rsc_stream(fixture_text(name))
    m = re.search(
        r'"content"\s*:\s*"((?:[^"\\]|\\.)*<a href=\\"/assets/[^"\\]*(?:[^"\\]|\\.)*)"', rsc
    )
    assert m, "fixture no longer contains a content blurb with an /assets/ link"
    return json.loads('"' + m.group(1) + '"')


def test_live_content_blurb_feeds_the_working_helpers():
    """bs4/lxml + penalty/date helpers behave on real PDPC markup."""
    html = _live_content_html("enforcement_detail_commission.html")
    text = ed._html_to_text(html)
    assert len(text) > 50
    assert text.replace(" ", "").endswith("Clickhereformoreinformation.")
    soup_links = re.findall(r'href="(/assets/[^"]+)"', html)
    assert soup_links and soup_links[0].endswith(".pdf")
    rsc = ed._decode_rsc_stream(fixture_text("enforcement_detail_commission.html"))
    assert re.search(r"Published on \",\"(\d{1,2} [A-Za-z]+ \d{4})\"", rsc)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REAL BUG (site drift): _parse_detail_page hardcodes RSC rows 22/24, but the live "
        "pages now carry the date in row 33 and the content blurb in row 35, so summary, "
        "pdf_url and decision_date come back empty. Remove this marker when the parser is fixed."
    ),
)
def test_parse_detail_page_on_live_commission_page():
    detail = ed._parse_detail_page(fixture_text("enforcement_detail_commission.html"))
    assert detail["decision_date"] == "2026-10-26"
    assert detail["summary"]
    assert detail["pdf_url"].startswith(f"{ed.BASE_URL}/assets/")
