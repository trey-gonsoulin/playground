"""Chronicling America search. The fixture mirrors a live loc.gov response
(fields as lists, IIIF image URLs, pagination block)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from genealogy import _newspapers as newspapers

IIIF = (
    "https://tile.loc.gov/image-services/iiif/service:ndnp:vtu:batch_vtu_kirby_ver02"
    ":data:sn84022552:0020219722A:1870022501:0665"
)
RESULT = {
    "title": "Image 2 of National opinion (Bradford, Vt.), February 25, 1870",
    "date": "1870-02-25",
    "partof_title": ["national opinion (bradford, vt.) 1865-1874"],
    "location_city": ["bradford"],
    "location_county": ["orange"],
    "location_state": ["vermont"],
    "description": [
        "Rational Opinion ... lecture by Mr. Samuel Clemens (Mark Twain) ..."
    ],
    "id": "http://www.loc.gov/resource/sn84022552/1870-02-25/ed-1/?sp=2",
    "image_url": [
        f"{IIIF}/full/pct:6.25/0/default.jpg#h=446&w=326",
        f"{IIIF}/full/pct:12.5/0/default.jpg#h=892&w=653",
        "https://tile.loc.gov/text-services/word-coordinates-service?segment=x",
    ],
}


@pytest.fixture
def api(monkeypatch):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        )
        body = {"results": [RESULT], "pagination": {"current": 1, "total": 2, "of": 5}}
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(newspapers, "_http", lambda: client)
    return calls


def test_search_builds_query_and_flattens_results(api):
    out = newspapers.search(
        "samuel clemens", start_date="1870", end_date="1871-06-30", state="Vermont"
    )
    sent = api[-1]
    assert sent["qs"] == "samuel clemens" and sent["ops"] == "PHRASE"
    assert (sent["start_date"], sent["end_date"]) == ("1870-01-01", "1871-06-30")
    assert sent["location_state"] == "vermont" and sent["fo"] == "json"

    assert (out["total"], out["pages"]) == (5, 2)
    (r,) = out["results"]
    assert r["newspaper"] == "national opinion (bradford, vt.) 1865-1874"
    assert r["location"] == "Bradford, Orange, Vermont"
    assert "Samuel Clemens" in r["ocr_excerpt"]
    assert r["image_url"] == f"{IIIF}/full/pct:50/0/default.jpg"


def test_all_words_mode_and_validation(api):
    newspapers.search("clemens samuel", phrase=False, count=500, page=0)
    sent = api[-1]
    assert (sent["ops"], sent["c"], sent["sp"]) == ("AND", "100", "1")
    with pytest.raises(ValueError):
        newspapers.search("  ")


def test_requests_only_needed_sections_and_reports_timeouts(api, monkeypatch):
    newspapers.search("walsh")
    assert api[-1]["at"] == "results,pagination"

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(slow))
    monkeypatch.setattr(newspapers, "_http", lambda: client)
    with pytest.raises(ValueError, match="shorter date range"):
        newspapers.search("walsh")
