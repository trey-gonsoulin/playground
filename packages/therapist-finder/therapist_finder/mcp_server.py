"""MCP server exposing therapist search tools via Streamable HTTP transport."""

from mcp.server.fastmcp import FastMCP

from therapist_finder._scrapers import search_inclusive_therapists, search_psychology_today

mcp = FastMCP(
    "therapist-finder",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    host="0.0.0.0",
)


@mcp.tool()
def search_therapists(
    location: str,
    specialties: list[str] | None = None,
    insurance: list[str] | None = None,
    telehealth: bool = False,
    lgbtq: bool = False,
    issue: str | None = None,
    sources: list[str] | None = None,
    max_results: int = 20,
) -> dict:
    """Search online therapist directories for providers near a given location.

    Searches Inclusive Therapists and Psychology Today in parallel and returns
    a combined list of matching providers.

    Args:
        location: City and state abbreviation in 'City, ST' format, e.g. 'New York, NY'.
        specialties: List of specialties or issues to filter by, e.g. ['anxiety', 'trauma'].
            Accepted values include: anxiety, depression, trauma, ptsd, adhd, grief, lgbtq,
            bipoc, couples, family, addiction, eating disorders, bipolar, ocd, autism,
            transgender, teens, children, telehealth.
        insurance: List of insurance providers to filter by, e.g. ['aetna', 'blue cross'].
        telehealth: If True, filter for providers who offer telehealth/online sessions.
        lgbtq: If True, filter for providers who are LGBTQ-affirming (Psychology Today).
        issue: Single primary issue for Psychology Today's issue filter (e.g. 'anxiety').
            If omitted and specialties is provided, the first specialty is used.
        sources: Which directories to search. Defaults to both. Options:
            'inclusive_therapists', 'psychology_today'.
        max_results: Maximum number of results to return per source (default 20).
    """
    active_sources = set(sources or ["inclusive_therapists", "psychology_today"])
    results = []
    errors: dict[str, str] = {}

    if "inclusive_therapists" in active_sources:
        try:
            it_results = search_inclusive_therapists(
                location=location,
                specialties=list(specialties or []),
                insurance=list(insurance or []),
                telehealth=telehealth,
                max_results=max_results,
            )
            results.extend(r.model_dump() for r in it_results)
        except Exception as exc:
            errors["inclusive_therapists"] = str(exc)

    if "psychology_today" in active_sources:
        try:
            # Use issue param directly, or fall back to first specialty
            pt_issue = issue
            if not pt_issue and specialties:
                pt_issue = specialties[0]

            pt_results = search_psychology_today(
                location=location,
                issue=pt_issue,
                insurance=insurance[0] if insurance else None,
                telehealth=telehealth,
                lgbtq=lgbtq,
                max_results=max_results,
            )
            results.extend(r.model_dump() for r in pt_results)
        except Exception as exc:
            errors["psychology_today"] = str(exc)

    return {
        "results": results,
        "total": len(results),
        **({"errors": errors} if errors else {}),
    }
