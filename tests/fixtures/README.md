# Test fixtures

All fixtures are static; tests never touch the network.

- `*_listing.json`: first three items of the real listing API
  (`/api/listing-api?...itemsperpage=3&page=1`), captured 2026-11-08. `totalItems`
  is the live total at capture time.
- `*_detail_*.html`: real detail pages captured 2026-11-08 and trimmed to the Next.js
  RSC rows the parsers read (see the HTML comment at the top of each file). Page
  chrome, scripts and the site header/footer were dropped; one cookie-banner row
  (carries `date_created` + `content`) is kept deliberately because
  `_extract_content_html` has to ignore it.
- `docling_response.json`: synthetic docling-serve `/v1/convert/file` response
  (made-up text, not a PDPC document).

The enforcement detail layout the parser expects (rows 22 and 24) is built in
`tests/conftest.py::rsc_page`, because the live site no longer serves that layout
(see the strict-xfail tests in `test_enforcement_decisions_parsing.py`).
