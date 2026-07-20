"""Scrapers for inclusivetherapists.com and psychologytoday.com."""

import json
import re
from typing import Any

import httpx

from therapist_finder._models import TherapistResult

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# US state abbreviation → full-name slug used in IT URLs
_IT_STATE_SLUGS: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina",
    "SD": "south-dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district-of-columbia",
}

# Common human-readable specialty terms → IT filter slug
_IT_SPECIALTY_SLUGS: dict[str, str] = {
    "anxiety": "anxiety",
    "depression": "depression",
    "trauma": "trauma",
    "ptsd": "post-traumatic-stress-disorder-ptsd",
    "adhd": "adhd",
    "grief": "grief-or-loss",
    "lgbtq": "lgbq-sexuality-identity",
    "bipoc": "person-of-color",
    "couples": "couples-marriage-romantic-sexual-relationships",
    "family": "family-therapy-counseling",
    "addiction": "addiction",
    "eating disorders": "disordered-eating-disorders-food-relationships",
    "bipolar": "bipolar-disorder",
    "ocd": "obsessive-compulsive-disorder-ocd",
    "autism": "autism-spectrum",
    "transgender": "transgender",
    "teens": "therapy-counseling-for-teens-adolescents",
    "children": "therapy-counseling-for-children",
    "telehealth": "virtual-video-online-therapy-counseling-coaching-teletherapy",
}

# Common insurance names → IT filter slug
_IT_INSURANCE_SLUGS: dict[str, str] = {
    "aetna": "aetna",
    "blue cross": "blue-cross",
    "blue cross blue shield": "blue-cross-blue-shield",
    "cigna": "cigna",
    "united": "unitedhealthcare",
    "unitedhealthcare": "unitedhealthcare",
    "humana": "humana",
    "medicare": "medicare",
    "medicaid": "medicaid",
    "tricare": "tricare",
    "anthem": "anthem",
    "kaiser": "kaiser",
    "optum": "optum",
    "magellan": "magellan-behavioral-health",
}


def _slugify(text: str) -> str:
    return re.sub(r"\s+", "-", text.strip().lower())


def _parse_location(location: str) -> tuple[str, str]:
    """Parse 'City, ST' into (city, state_abbr). Raises ValueError if format unrecognized."""
    m = re.match(r"^(.+),\s*([A-Za-z]{2})\s*$", location.strip())
    if not m:
        raise ValueError(
            f"Location must be in 'City, ST' format (e.g. 'New York, NY'), got: {location!r}"
        )
    return m.group(1).strip(), m.group(2).upper()


def _it_location_path(location: str) -> str:
    """Convert 'City, ST' to IT URL path segment like 'new-york/new-york'."""
    city, state = _parse_location(location)
    state_slug = _IT_STATE_SLUGS.get(state)
    if not state_slug:
        raise ValueError(f"Unsupported US state abbreviation: {state!r}")
    city_slug = _slugify(city)
    return f"{state_slug}/{city_slug}"


def _pt_location_path(location: str) -> str:
    """Convert 'City, ST' to PT URL path segment like 'ny/new-york'."""
    city, state = _parse_location(location)
    city_slug = _slugify(city)
    return f"{state.lower()}/{city_slug}"


def _it_resolve_filter_slug(term: str) -> str:
    """Map a human-readable term to an IT filter slug, or return as-is if already a slug."""
    normalized = term.lower().strip()
    return _IT_SPECIALTY_SLUGS.get(normalized) or _IT_INSURANCE_SLUGS.get(normalized) or _slugify(term)


# ── Inclusive Therapists ──────────────────────────────────────────────────────

def _parse_it_card(card_html: str) -> TherapistResult | None:
    name_m = re.search(r'member-search-full-name">\s*(.*?)\s*</span>', card_html, re.DOTALL)
    desc_m = re.search(r'member-search-description">\s*(.*?)\s*</p>', card_html, re.DOTALL)
    loc_m = re.search(r'member-search-location[^>]*>(.*?)</span>', card_html, re.DOTALL)
    href_m = re.search(r'href="(/[^"]+)"[^>]*>\s*View Profile', card_html, re.IGNORECASE)
    avail_m = re.search(r"class='(available_\w+)'", card_html)

    if not name_m or not href_m:
        return None

    name = re.sub(r"\s+", " ", name_m.group(1)).strip()
    if loc_m:
        loc_text = re.sub(r"<[^>]+>", " ", loc_m.group(1))
        loc_text = re.sub(r",\s*United States\s*$", "", loc_text, flags=re.IGNORECASE)
        location = re.sub(r"\s+", " ", loc_text).strip().strip(",").strip()
    else:
        location = None
    description = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else None

    accepting = None
    if avail_m:
        cls = avail_m.group(1)
        accepting = cls == "available_on"

    return TherapistResult(
        name=name,
        location=location,
        description=description,
        profile_url=f"https://www.inclusivetherapists.com{href_m.group(1)}",
        accepting_new_clients=accepting,
        source="inclusive_therapists",
    )


def _parse_it_html(html: str) -> list[TherapistResult]:
    cards = re.split(r'(?=<div[^>]*class="row-fluid member_results)', html)
    results = []
    for card in cards[1:]:  # skip header/CSS chunk before first card
        r = _parse_it_card(card)
        if r:
            results.append(r)
    return results


def search_inclusive_therapists(
    location: str,
    specialties: list[str] | None = None,
    insurance: list[str] | None = None,
    telehealth: bool = False,
    max_results: int = 20,
) -> list[TherapistResult]:
    loc_path = _it_location_path(location)
    base_url = f"https://www.inclusivetherapists.com/{loc_path}"

    # Build filter query params
    filters: dict[str, str] = {}
    for term in specialties or []:
        slug = _it_resolve_filter_slug(term)
        filters[slug] = "on"
    for ins in insurance or []:
        slug = _it_resolve_filter_slug(ins)
        filters[slug] = "on"
    if telehealth:
        filters["virtual-video-online-therapy-counseling-coaching-teletherapy"] = "on"

    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(base_url, params=filters)
        resp.raise_for_status()
        results = _parse_it_html(resp.text)

        # Extract the queryString JS object the page embedded for pagination
        qs_m = re.search(r"queryString\s*=\s*({[^;]+})", resp.text)
        query_string_obj: dict[str, Any] = json.loads(qs_m.group(1)) if qs_m else {
            "sized": 0,
            "mysql_real_escape_string_runned": "1",
            "form": "myform",
            "formname": "member_login",
            "dowiz": 1,
            "save": 1,
            "url_origin_pars": f"/{loc_path}",
        }
        # Merge any filters into the query string object for correct pagination
        query_string_obj.update(filters)

        page = 2
        while len(results) < max_results:
            form_data = {
                "dc_id": "1",
                "header_type": "html",
                "request_type": "POST",
                "currentPage": str(page),
                "dataType": "10",
                "queryString": json.dumps(query_string_obj),
                "profId": "",
                "servId": "null",
                "countryId": "",
                "stateId": "",
                "cityId": "",
                "levId": "",
                "seed": "",
                "profsPost": json.dumps({"new_filename": loc_path.split("/")[-1]}),
                "widget_name": "Add-On - Bootstrap Theme - Search - Lazy Loader",
            }
            widget_resp = client.post(
                "https://www.inclusivetherapists.com/wapi/widget",
                data=form_data,
            )
            widget_resp.raise_for_status()

            pages_m = re.search(r'data-pages="(\d+)"', widget_resp.text)
            total_pages = int(pages_m.group(1)) if pages_m else 0
            page_results = _parse_it_html(widget_resp.text)
            if not page_results:
                break

            # The widget ignores url_origin_pars for filtering — it returns global
            # results. Keep only profiles that belong to the requested city URL path.
            local_results = [
                r for r in page_results
                if f"inclusivetherapists.com/{loc_path}/" in r.profile_url
            ]
            if not local_results:
                break
            results.extend(local_results)
            if page >= total_pages:
                break
            page += 1

    return results[:max_results]


# ── Psychology Today ──────────────────────────────────────────────────────────

def _deref(data: list, val: Any) -> Any:
    """Dereference a Nuxt flat-array index."""
    if isinstance(val, int) and val < len(data):
        return data[val]
    return val


def _parse_pt_html(html: str) -> list[TherapistResult]:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    state_json = next(
        (s for s in scripts if s.startswith("[") and '"searchLocations"' in s),
        None,
    )
    if not state_json:
        return []

    data: list = json.loads(state_json)
    results: list[TherapistResult] = []

    for item in data:
        if not isinstance(item, dict) or "firstName" not in item:
            continue

        first = _deref(data, item.get("firstName"))
        last = _deref(data, item.get("lastName"))
        if not isinstance(first, str) or not isinstance(last, str):
            continue

        # Credential suffixes (MA, LCSW, PhD, etc.) — skip HealthRoles.* entries
        suffix_refs = _deref(data, item.get("suffixes"))
        suffix_labels: list[str] = []
        if isinstance(suffix_refs, list):
            for sr in suffix_refs:
                obj = _deref(data, sr)
                if isinstance(obj, dict):
                    lbl = _deref(data, obj.get("label"))
                    if isinstance(lbl, str) and not lbl.startswith("HealthRoles."):
                        suffix_labels.append(lbl)

        # Location
        loc_ref = _deref(data, item.get("primaryLocation"))
        location = None
        if isinstance(loc_ref, dict):
            city = _deref(data, loc_ref.get("cityName"))
            region = _deref(data, loc_ref.get("regionCode"))
            postal = _deref(data, loc_ref.get("postalCode"))
            parts = [p for p in [city, region, postal] if isinstance(p, str)]
            location = ", ".join(parts) if parts else None

        # Profile URL — replace Nuxt template placeholders
        url_path = _deref(data, item.get("urlPath"))
        if isinstance(url_path, str):
            url_path = url_path.replace("[COUNTRY_CODE]", "us").replace("[PROFILE_CLASS]", "therapists")
            profile_url = f"https://www.psychologytoday.com/{url_path}"
        else:
            continue

        # Personal statement
        stmts = _deref(data, item.get("personalStatements"))
        description = None
        if isinstance(stmts, list) and stmts:
            stmt = _deref(data, stmts[0])
            if isinstance(stmt, dict):
                p1 = _deref(data, stmt.get("paragraph1"))
                if isinstance(p1, str):
                    description = p1

        # Accepting / telehealth
        accepting_raw = _deref(data, item.get("accepting_appointments"))
        accepting = accepting_raw == "YES" if isinstance(accepting_raw, str) else None

        appt_types = _deref(data, item.get("appointmentTypes"))
        tele = None
        if isinstance(appt_types, dict):
            online_val = _deref(data, appt_types.get("online"))
            if isinstance(online_val, bool):
                tele = online_val

        results.append(TherapistResult(
            name=f"{first} {last}",
            credentials=", ".join(suffix_labels) if suffix_labels else None,
            location=location,
            profile_url=profile_url,
            description=description,
            accepting_new_clients=accepting,
            telehealth=tele,
            source="psychology_today",
        ))

    return results


def search_psychology_today(
    location: str,
    issue: str | None = None,
    insurance: str | None = None,
    telehealth: bool = False,
    lgbtq: bool = False,
    max_results: int = 20,
) -> list[TherapistResult]:
    loc_path = _pt_location_path(location)
    url = f"https://www.psychologytoday.com/us/therapists/{loc_path}"

    params: dict[str, str] = {}
    if issue:
        params["issue"] = issue.lower()
    if insurance:
        params["insurance"] = insurance.lower()
    if telehealth:
        params["telehealth"] = "true"
    if lgbtq:
        params["lgbta"] = "true"

    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()

    return _parse_pt_html(resp.text)[:max_results]
