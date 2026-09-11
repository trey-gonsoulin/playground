"""MCP server exposing Elden Ring build and lore search tools."""

from mcp.server.fastmcp import FastMCP

import elden_ring._client as _os

mcp = FastMCP(
    "elden-ring",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    host="0.0.0.0",
)


@mcp.tool()
def start_search_service() -> dict:
    """Start the Elden Ring search service if it's not running.

    The search backend runs on a stopped EC2 instance to save cost. Call this
    tool first whenever search tools return connection errors. It starts the
    instance and blocks until OpenSearch is healthy (typically 2-3 minutes on
    a cold start). Returns immediately if the service is already running.

    Returns {"status": "ready"} on success or {"status": "timeout", "message": "..."}
    if the instance did not become healthy within 4 minutes.
    """
    try:
        endpoint = _os.start_instance(timeout_seconds=240)
        return {"status": "ready", "endpoint": endpoint}
    except TimeoutError as e:
        return {"status": "timeout", "message": str(e)}


@mcp.tool()
def search_entities(
    query: str,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search Elden Ring entities by name, description, location, or tags.

    Results are ranked by relevance; name matches score 3× higher than body text.
    Fuzzy matching handles minor typos.

    If this tool returns a connection error, call start_search_service() first,
    then retry.

    Args:
        query: Free-text search. Examples by use case:
            - Build queries: "bleed katana", "faith scaling halberd", "high poise armor"
            - Spell/AoW lookup: "gravity sorcery", "ash of war lifesteal"
            - Enemy info: "Margit drops", "what enemies are in Stormveil Castle"
            - Merchant queries: "who sells Stonesword Key", "Patches inventory"
            - NPC location: "where is Ranni", "Millicent questline"
            - Lore: "Ranni lore", "Marika dialogue", "Elden Ring story"
        entity_type: Narrow to one category:
            weapon       — all weapons with stats, scaling, and requirements
            armor        — all armor pieces with weight and defense values
            spell        — sorceries and incantations with FP cost and requirements
            ash_of_war   — weapon skills / ashes of war with effect descriptions
            item         — talismans and other equippable items
            merchant     — NPC vendor inventories with item names and rune prices;
                           search by item name to find who sells it, or by merchant
                           name / location to get their full stock
            npc          — NPC character profiles with location and role
            npc_dialogue — full NPC conversation transcripts (searchable by quote)
            enemy        — enemies and bosses with HP, locations, and drop tables
        patch_version: Filter to a specific erdb source version (e.g. "1.07.0").
            This is the erdb snapshot version, not necessarily the patch when
            content was introduced. Use list_patch_versions() to see what's loaded.
            Omit to return one result per entity (latest indexed version); pass
            a specific version to scope results to that snapshot only.
        limit: Maximum results to return (default 20, max 100).

    Returns a list of entity documents, each with at minimum: entity_type, name,
    patch_version, source, description. Use get_entity() for the full document of
    a specific named entity. Item documents may include cross-reference edge fields:
      dropped_by — enemy/boss names that drop this item
      sold_by    — merchant names that sell this item
    """
    return _os.search(_os.get_client(), query, entity_type, patch_version, min(limit, 100))


@mcp.tool()
def get_entity(name: str, entity_type: str | None = None) -> dict | None:
    """Retrieve the full data document for a named Elden Ring entity.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        name: Exact entity name (case-sensitive), e.g. "Rivers of Blood".
        entity_type: Optional type hint to disambiguate if two entities share
            a name across categories (e.g. a boss and a lore entry).

    Returns the full document dict, or null if the entity is not in the index.
    """
    return _os.get_entity(_os.get_client(), name, entity_type)


@mcp.tool()
def list_entity_types() -> list[str]:
    """List the entity types currently loaded in the index.

    Returns a list of type strings, e.g. ["weapon", "armor", "spell", "boss"].
    Use these values as the entity_type argument to search_entities().

    If this tool returns a connection error, call start_search_service() first.
    """
    return _os.list_entity_types(_os.get_client())


@mcp.tool()
def list_patch_versions() -> list[str]:
    """List the patch versions currently loaded in the index, sorted oldest-first.

    Use these values as the v1/v2 arguments to diff_entities(), or as the
    patch_version filter in search_entities().

    If this tool returns a connection error, call start_search_service() first.
    """
    return _os.list_patch_versions(_os.get_client())


@mcp.tool()
def search_entities_literal(
    pattern: str,
    fields: list[str] | None = None,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 200,
) -> dict:
    """Search for entities containing an exact literal substring across text fields.

    Uses phrase matching rather than fuzzy relevance ranking, so the pattern must
    appear verbatim in the text. Suited for corpus-wide morpheme and construction
    tracking where exact counts matter more than relevance ordering.

    For Japanese text the standard analyzer produces character-level (unigram) tokens,
    so phrase matching correctly handles CJK and hiragana substrings like "象った".

    Note: regex patterns are not supported. The text fields use an analyzed mapping
    that does not allow regex across multi-character sequences. A reindex with
    keyword subfields would be needed to enable regex mode.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        pattern: Literal substring to find, e.g. "象った", "という", "Eternal Dragon".
        fields: Which fields to search. Defaults to all six text fields:
            name, description, text_content, name_ja, description_ja, text_content_ja.
        entity_type: Narrow to one entity category (weapon, armor, spell, enemy, etc.).
        patch_version: Filter to a specific patch snapshot (e.g. "1.10.0"). Omit to
            search across all patches and return one result per entity (latest version).
            Use list_patch_versions() to see available versions.
        limit: Maximum results to return (default 200, max 500).

    Returns a dict with:
        total: int — distinct entity count when no patch_version is given (deduplicated);
            raw document count when a specific patch_version is specified.
        results: list of entity documents
    """
    return _os.search_literal(_os.get_client(), pattern, fields, entity_type, patch_version, min(limit, 500))


@mcp.tool()
def text_changed_between(
    entity_type: str,
    field: str,
    v1: str,
    v2: str,
) -> list[dict]:
    """Find all entities of a type where a specific field changed between two patch versions.

    Runs two bulk queries (one per version) and compares at the application layer.
    Suited for corpus-wide analysis — e.g. "which talismans had their description
    rewritten between 1.02.1 and 1.10.0?". Two calls (one for description, one for
    description_ja) reveal whether changes are lore rewrites or localisation-only.

    Only entities present in both versions are included. To check whether an entity
    was added or removed between patches, use diff_entities().

    Call list_patch_versions() first to see what's loaded.
    Call list_entity_types() to see valid entity_type values.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        entity_type: Entity category to scan (weapon, armor, spell, item, enemy, etc.).
        field: Field to compare, e.g. "description", "description_ja", "text_content",
            "location", "effect". Any indexed field works; missing values compare as null.
        v1: Older patch version, e.g. "1.02.1".
        v2: Newer patch version, e.g. "1.10.0".

    Returns a list of dicts, one per changed entity, each with:
        name: entity name
        text_before: field value at v1 (null if absent)
        text_after: field value at v2 (null if absent)
    Sorted alphabetically by name.
    """
    return _os.text_changed_between(_os.get_client(), entity_type, field, v1, v2)


@mcp.tool()
def diff_entities(
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
) -> dict:
    """Compare an entity's fields between two patch versions.

    Returns which fields changed (with before/after values) and which were
    unchanged. Useful for tracking description rewrites, stat adjustments, or
    location changes between patches.

    Call list_patch_versions() first to see what's loaded.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        name: Exact entity name (case-sensitive), e.g. "Longtail Cat Talisman".
        v1: Older patch version, e.g. "1.06.0".
        v2: Newer patch version, e.g. "1.07.0".
        entity_type: Optional type hint to disambiguate if two entities share a name.

    Returns a dict with:
        changed: bool — whether any fields differ
        changed_fields: {field: {v1: old_value, v2: new_value}} for each changed field
        unchanged_fields: [field, ...] for fields present in both with identical values
    """
    return _os.diff_entities(_os.get_client(), name, v1, v2, entity_type)
