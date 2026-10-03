"""Historic US newspaper search via the Library of Congress loc.gov JSON API.

Chronicling America (https://www.loc.gov/collections/chronicling-america/)
holds 20M+ OCR'd newspaper pages, 1770–1963. No key is needed; responses are
requested with ``fo=json``. Each result is one newspaper page.

Page images come from the IIIF image service (``tile.loc.gov/image-services``);
the storage-service PDF links refuse non-browser clients, so for attaching a
page we use a 50%-scale IIIF JPEG (readable, ~1–2 MB).

Latency: loc.gov full-text search is slow and erratic (measured 4–50 s for the
same kinds of query, with no caching benefit on repeats). ``at=results,
pagination`` trims the response to what we use, which removes most of the
time when the search itself is quick. The request timeout defaults to 25 s so
it fits under API Gateway's 30 s limit in Lambda; set NEWSPAPER_TIMEOUT higher
when running locally.
"""

import os
import re
from functools import cache

import httpx

COLLECTION = "https://www.loc.gov/collections/chronicling-america/"
_IIIF_SIZE = re.compile(r"/full/pct:[\d.]+/")


@cache
def _http() -> httpx.Client:
    return httpx.Client(
        timeout=float(os.environ.get("NEWSPAPER_TIMEOUT", "25")),
        follow_redirects=True,
        headers={"User-Agent": "genealogy-mcp/0.1 (personal research tool)"},
    )


def _first(value: list[str] | str | None) -> str | None:
    """loc.gov returns most fields as lists; take the first value."""
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _page_image(image_urls: list[str]) -> str | None:
    """Pick the IIIF page image and rescale it to 50%."""
    iiif = next((u for u in image_urls if "/image-services/iiif/" in u), None)
    if iiif is None:
        return None
    return _IIIF_SIZE.sub("/full/pct:50/", iiif.split("#", 1)[0])


def _excerpt(text: str, terms: list[str], width: int = 160) -> str:
    """OCR text around the first matching term, else the start of the text.

    loc.gov only returns the first ~1000 characters of a page's OCR, so the
    match is often not in it; OCR is noisy either way.
    """
    flat = " ".join(text.split())
    lower = flat.lower()
    hit = min((i for t in terms if (i := lower.find(t.lower())) >= 0), default=0)
    start = max(0, hit - width)
    return ("…" if start else "") + flat[start : hit + width] + "…"


def search(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    state: str | None = None,
    phrase: bool = True,
    count: int = 20,
    page: int = 1,
) -> dict:
    """Full-text search of newspaper pages.

    Dates are YYYY-MM-DD (or YYYY, expanded to the whole year). ``state`` is a
    full, case-insensitive US state name. ``phrase`` searches the exact
    phrase; otherwise all words must appear.
    """
    if not query.strip():
        raise ValueError("query is required")
    params = {
        "fo": "json",
        "at": "results,pagination",
        "dl": "page",
        "searchType": "advanced",
        "qs": query,
        "ops": "PHRASE" if phrase else "AND",
        "start_date": _expand(start_date, "01-01"),
        "end_date": _expand(end_date, "12-31"),
        "location_state": state.lower() if state else None,
        "c": max(1, min(count, 100)),
        "sp": max(1, page),
    }
    try:
        resp = _http().get(COLLECTION, params={k: v for k, v in params.items() if v})
    except httpx.TimeoutException:
        raise ValueError(
            "The Library of Congress search timed out (it often takes 10-50 s). "
            "Narrow it with a shorter date range or a state, then try again."
        ) from None
    resp.raise_for_status()
    data = resp.json()
    terms = [query] if phrase else query.split()
    results = [
        {
            "title": r.get("title"),
            "newspaper": _first(r.get("partof_title")),
            "date": r.get("date"),
            "location": ", ".join(
                x.title()
                for x in (
                    _first(r.get("location_city")),
                    _first(r.get("location_county")),
                    _first(r.get("location_state")),
                )
                if x
            )
            or None,
            "ocr_excerpt": _excerpt(" ".join(r.get("description") or []), terms),
            "page_url": r.get("id"),
            "image_url": _page_image(r.get("image_url") or []),
        }
        for r in data.get("results", [])
    ]
    pagination = data.get("pagination") or {}
    return {
        "total": pagination.get("of", len(results)),
        "page": pagination.get("current", page),
        "pages": pagination.get("total"),
        "results": results,
    }


def _expand(date: str | None, suffix: str) -> str | None:
    if date and len(date) == 4 and date.isdigit():
        return f"{date}-{suffix}"
    return date
