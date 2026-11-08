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


@pytest.mark.parametrize(
    "title",
    [
        "Breach of the Protection and Accountability Obligations by KLP LLP",
        "Breach of the Accountability and Protection Obligations by KLP LLP",
        "Breach of the Consent, Notification and Purpose Limitation Obligations by KLP LLP",
        "Breaches of the Protection Obligation by KLP LLP",
    ],
)
def test_title_to_organisation_multi_obligation_title(title):
    """Real listing titles name several obligations; the old 'Breach of ' prefix mangled them."""
    assert ed._title_to_organisation(title) == "KLP LLP"


@pytest.mark.parametrize(
    "title, organisation",
    [
        (
            "Breach of the Protection and Accountability Obligations by MCST 3593 and "
            "Breach of the Protection Obligation by New-E Security",
            "MCST 3593; New-E Security",
        ),
        (
            "Breach of the Protection and Retention Limitation Obligations by Times Software, "
            "Breach of the Protection Obligation by Dentons and TMF",
            "Times Software; Dentons and TMF",
        ),
        (
            "Breach of the Consent Obligation by Greatearth Corporation, "
            "No Breach of the PDPA by Progressive Builders",
            "Greatearth Corporation; Progressive Builders",
        ),
        ("Breach of Protection Obligation by Hello Travel", "Hello Travel"),
        ("Breach of the Protection Obligation by  Jean Yip Salon", "Jean Yip Salon"),
        (
            "Breach of Section 48B of the PDPA (Prohibition on Use of Dictionary Attacks) "
            "by an individual",
            "an individual",
        ),
        ("Voluntary Undertaking by Lian Beng Group Pte Ltd and subsidiaries",
         "Lian Beng Group Pte Ltd and subsidiaries"),
    ],
)
def test_title_to_organisation_real_title_shapes(title, organisation):
    """Shapes found in the production table (2026-11-08)."""
    assert ed._title_to_organisation(title) == organisation


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


# --- detail page: synthetic layout (rows 22 / 24, the 2025 row numbering) -------


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


def test_parse_detail_page_on_live_commission_page():
    detail = ed._parse_detail_page(fixture_text("enforcement_detail_commission.html"))
    assert detail["decision_date"] == "2026-10-26"
    assert detail["summary"]
    assert detail["pdf_url"] == f"{ed.BASE_URL}/assets/212d471b-723f-4ba2-b7ce-ce0754e71b85.pdf"
    assert detail["summary"].startswith("A financial penalty of $178,000 was imposed on Courts")
    assert detail["penalty_amount"] == 178000.0
    assert "Click here for more information." in detail["summary"]


def test_parse_detail_page_on_live_voluntary_undertaking_page():
    """VU pages carry their full text inline, via a "$<row>" reference to a T row."""
    detail = ed._parse_detail_page(fixture_text("enforcement_detail_voluntary_undertaking.html"))
    assert detail["decision_date"] == "2026-10-26"
    assert detail["summary"].startswith("DCS Card Centre Pte Ltd (the “Organisation”) notified")
    assert "DCS Card Centre Pte Ltd" in detail["summary"]
    assert "cookies" not in detail["summary"]


def test_html_to_text_keeps_words_apart_around_links():
    html = '<p>Click <a href="/assets/x.pdf">here</a> for more information.</p>'
    assert ed._html_to_text(html) == "Click here for more information."


def test_parse_detail_page_falls_back_to_cta_asset():
    html = rsc_page(
        {
            "33": '["$","span",null,{"className":"page-banner__date","children":["Published on ","2 Jan 2026"]}]',
            "35": json.dumps({"content": "<p>Summary text without any link at all.</p>"}),
            "36": '{"cta":{"type":"external","url":"/assets/decision-uuid","label":"Read"}}',
        }
    )
    detail = ed._parse_detail_page(html)
    assert detail["decision_date"] == "2026-01-02"
    assert detail["pdf_url"] == f"{ed.BASE_URL}/assets/decision-uuid"


def test_parse_detail_page_skips_meta_and_cookie_content():
    """Full live pages carry <meta content=...> rows and the cookie banner before/after
    the blurb; only the component's data.content is the summary."""
    html = rsc_page(
        {
            "15": '[["$","meta","0",{"charSet":"utf-8"}],["$","meta","1",{"name":"viewport","content":"width=device-width, initial-scale=1"}]]',
            "35": '["$","$L37",null,{"data":{"content":"<p>A financial penalty of $17,500 was imposed on KLP LLP. Click <a href=\\"/assets/d.pdf\\">here</a>.</p>"}}]',
            "36": '["$","$L39",null,{"data":{"date_created":"2025-12-02","content":"<p>To offer you a better experience, this site uses cookies.</p>"}}]',
        }
    )
    detail = ed._parse_detail_page(html)
    assert detail["summary"] == "A financial penalty of $17,500 was imposed on KLP LLP. Click here."
    assert detail["pdf_url"] == f"{ed.BASE_URL}/assets/d.pdf"
    assert detail["penalty_amount"] == 17500.0
