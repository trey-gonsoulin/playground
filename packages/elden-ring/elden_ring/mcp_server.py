"""MCP server exposing Elden Ring build and lore search tools."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import elden_ring._client as _os

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

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


@mcp.tool(annotations=_READ_ONLY)
def search_entities(
    query: str,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    source: str | None = None,
) -> list[dict] | dict:
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
        include_fields: If provided, only these fields are returned per document (e.g.
            ["name", "sort_id"]). Reduces payload size for large result sets.
        count_only: If True, return {"total": N} instead of the full document list.
            N is the distinct-entity count when no patch_version is given, or the
            raw match count when a specific version is specified. Useful for counting
            query matches without fetching documents. For a type-level census
            (counting all entities of a given type without a search query), use
            search_entities_literal with count_only=True instead.
        source: If provided, restrict to documents from this data source (e.g.
            "erdb" or "fextralife-discord-bot"). Use list_patch_versions() to see
            which sources are loaded. Useful for getting clean counts without
            cross-source duplicates (e.g. source="erdb" for authoritative base-game data).

    Returns a list of entity documents when count_only is False, each with at minimum:
    entity_type, name, patch_version, source, description. Use get_entity() for the
    full document of a specific named entity. Item documents may include cross-reference
    edge fields:
      dropped_by — enemy/boss names that drop this item
      sold_by    — merchant names that sell this item

    Returns {"total": N} when count_only is True.
    """
    return _os.search(
        _os.get_client(),
        query,
        entity_type,
        patch_version,
        min(limit, 100),
        include_fields,
        count_only,
        source,
    )


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_READ_ONLY)
def list_menu_categories(
    entity_type: str | None = None,
    source: str | None = None,
) -> dict:
    """List the distinct menu_category values in the index with entity counts.

    menu_category reflects the in-game equipment menu grouping (e.g. "Straight Sword",
    "Reaper", "Head", "Consumables"). Counts are distinct-entity counts (not raw
    document counts), so multi-patch duplication does not inflate the numbers.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        entity_type: If provided, return {category: entity_count} for that type
            (e.g. entity_type="weapon" → {"Straight Sword": 26, "Reaper": 4, ...}).
            If omitted, return {entity_type: {category: entity_count}} for every
            type that has menu_category set — all data in one call.
        source: If provided, restrict counts to documents from this data source
            (e.g. source="erdb"). Use this to scope to the authoritative base-game
            layer and exclude cross-source duplicates. See list_patch_versions() for
            available source values.

    Returns:
        dict[str, int] when entity_type is given; dict[str, dict[str, int]] otherwise.
    """
    return _os.list_menu_categories(_os.get_client(), entity_type, source)


@mcp.tool(annotations=_READ_ONLY)
def list_entity_types() -> dict:
    """List the entity types currently loaded in the index.

    Returns {"entity_types": ["weapon", "armor", "spell", "boss", ...]}.
    Use these values as the entity_type argument to search_entities().

    If this tool returns a connection error, call start_search_service() first.
    """
    return {"entity_types": _os.list_entity_types(_os.get_client())}


@mcp.tool(annotations=_READ_ONLY)
def list_patch_versions() -> dict:
    """List the patch versions currently loaded in the index with their data sources.

    Use the version strings as v1/v2 arguments to diff_entities(), or as the
    patch_version filter in search_entities().

    If this tool returns a connection error, call start_search_service() first.

    Returns a dict with:
        versions: list[str] — version strings sorted oldest-first
        sources:  dict[str, str] — maps each version to its dominant source
                  (e.g. {"1.10.0": "erdb", "1.16.0": "fextralife-discord-bot"})
    """
    return _os._version_info(_os.get_client())


@mcp.tool(annotations=_READ_ONLY)
def analyze_text(text: str) -> dict:
    """Return the token stream for a text string under both indexed analyzers.

    Calls OpenSearch's _analyze API using the exact field paths that search_literal()
    applies, so the output reflects precisely what a phrase query will match:
    - standard: CJK unigram tokenization (default mode, use_kuromoji=False)
    - kuromoji_segmenter: dictionary segmentation, no lemmatization (use_kuromoji=True)

    Use this to diagnose unexpected zeros before concluding a morpheme is absent.
    In particular, single-kanji suru-verbs that lack IPADIC entries (e.g. 模す, 象る)
    may be split differently than expected — see the use_kuromoji docstring on
    search_entities_literal for details.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        text: Any string to analyze, e.g. "模した", "象る", "Eternal Dragon".

    Returns:
        standard: list[str] — tokens under standard CJK-unigram tokenization
        kuromoji_segmenter: list[str] — tokens under kuromoji segmentation-only mode
    """
    return _os.analyze_text(_os.get_client(), text)


@mcp.tool(annotations=_READ_ONLY)
def search_entities_literal(
    pattern: str | None = None,
    fields: list[str] | None = None,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 200,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    sort_id_gte: int | None = None,
    sort_id_lte: int | None = None,
    sort_id_mod: int | None = None,
    sort_id_remainder: int = 0,
    use_kuromoji: bool = False,
    patterns: list[str] | None = None,
    source: str | None = None,
) -> dict:
    """Search for entities containing an exact literal substring across text fields.

    Uses phrase matching rather than fuzzy relevance ranking, so the pattern must
    appear verbatim in the text. Suited for corpus-wide morpheme and construction
    tracking where exact counts matter more than relevance ordering.

    Omit pattern (or pass None) to enumerate all entities matching other filters
    without a text constraint — useful for structural queries like "all named weapons"
    via sort_id_mod, or census queries via count_only.

    For Japanese text there are two modes:
    - Default (use_kuromoji=False): standard CJK-unigram tokenization. Every character
      is its own token, so phrase matching finds any verbatim byte sequence — including
      multi-char strings like "象った" and particles like "という".
    - use_kuromoji=True: segmentation-only kuromoji tokenization (normal mode, no
      lemmatization or stopword removal). Tokens are dictionary morphemes, so a search
      for "象" only matches documents where 象 is a standalone word — it will NOT match
      象徴 (symbol) or 象牙 (ivory). Use this when counting a specific morpheme and
      false positives from compound words would inflate the count.

    Important: kuromoji mode uses IPADIC, which lacks entries for many game-specific
    verbs. Single-kanji suru-verbs (e.g. 模す, 象る, 擬す) are particularly affected:
    IPADIC splits 模した as 模 (noun) + し (suru conjugation) + た, so the stem 模し is
    never a token and a query for it returns zero. In these cases, use the bare kanji
    instead (模, not 模し) — and use analyze_text() to verify tokenization before
    trusting a zero result from kuromoji mode.

    Note: regex patterns are not supported.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        pattern: Literal substring to find, e.g. "象った", "という", "Eternal Dragon".
            Omit to enumerate all entities matching other filters (match_all mode).
        patterns: List of literal substrings to find (OR semantics). Use this for
            inflected Japanese verbs that require multiple stem queries — e.g.
            patterns=["象っ", "象ら", "象り", "象る"] returns all entities matching
            any inflection as a single deduplicated total. Combines with pattern if
            both are provided.
        fields: Which fields to search. Defaults to all six text fields:
            name, description, text_content, name_ja, description_ja, text_content_ja
            (or their .morph equivalents when use_kuromoji=True).
        entity_type: Narrow to one entity category (weapon, armor, spell, enemy, etc.).
        patch_version: Filter to a specific patch snapshot (e.g. "1.10.0"). Omit to
            search across all patches and return one result per entity (latest version).
            Use list_patch_versions() to see available versions.
        limit: Maximum results to return (default 200, max 500).
        include_fields: If provided, only these fields are returned per document (e.g.
            ["name", "sort_id"]). Reduces payload when full documents aren't needed.
        count_only: If True, return {"total": N} without a results list. Useful for
            census queries (e.g. count all weapons of a given type) without fetching
            any documents. N is distinct-entity count without patch_version, raw hit
            count with patch_version.
        sort_id_gte: Filter to entities with sort_id >= this value.
        sort_id_lte: Filter to entities with sort_id <= this value.
        sort_id_mod: If set, keep only entities where sort_id % sort_id_mod == sort_id_remainder.
            Example: sort_id_mod=1000, sort_id_remainder=0 matches every base named weapon
            (sort_id is a multiple of 1000 for named armaments, +N for upgrade variants).
        sort_id_remainder: Remainder for the modulo filter (default 0).
        use_kuromoji: If True, route Japanese fields through kuromoji morpheme segmentation
            so phrase queries respect dictionary word boundaries. Prevents single-kanji
            queries from matching compounds that contain that kanji as a sub-character.
            Has no effect on English fields. Default False (standard CJK-unigram mode).
            See the suru-verb caveat above before trusting zero results from this mode.
        source: If provided, restrict to documents from this data source (e.g. "erdb"
            or "fextralife-discord-bot"). Useful for clean corpus counts that exclude
            cross-source duplicates. See list_patch_versions() for source values.

    Returns a dict with:
        total: int — distinct entity count when no patch_version is given (deduplicated);
            raw document count when a specific patch_version is specified.
        results: list of entity documents (omitted when count_only is True).
    """
    return _os.search_literal(
        _os.get_client(),
        pattern,
        fields,
        entity_type,
        patch_version,
        min(limit, 500),
        include_fields,
        count_only,
        sort_id_gte,
        sort_id_lte,
        sort_id_mod,
        sort_id_remainder,
        use_kuromoji,
        patterns,
        source,
    )


@mcp.tool(annotations=_READ_ONLY)
def text_changed_between(
    entity_type: str,
    field: str,
    v1: str,
    v2: str,
    allow_cross_source: bool = False,
) -> list[dict] | dict:
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
        allow_cross_source: If True, allow comparing versions from different data sources
            (erdb vs fextralife). By default this is refused because cross-source diffs
            measure scrape differences, not game revisions.

    Returns a list of dicts, one per changed entity, each with:
        name: entity name
        text_before: field value at v1 (null if absent)
        text_after: field value at v2 (null if absent)
    Sorted alphabetically by name.

    Returns {"error": "..."} if either version is not loaded or sources differ.
    """
    return _os.text_changed_between(
        _os.get_client(), entity_type, field, v1, v2, allow_cross_source
    )


@mcp.tool(annotations=_READ_ONLY)
def diff_entities(
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
    allow_cross_source: bool = False,
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
        allow_cross_source: If True, allow comparing versions from different data sources
            (erdb vs fextralife). By default this is refused because cross-source diffs
            measure scrape differences, not game revisions.

    Returns a dict with:
        changed: bool — whether any fields differ
        changed_fields: {field: {v1: old_value, v2: new_value}} for each changed field
        unchanged_fields: [field, ...] for fields present in both with identical values

    Returns {"error": "..."} if either version is not loaded, sources differ, or
    the entity is not present in the requested versions.
    Error cases:
        patch version 'X' is not loaded → version not in the index
        'Name' not present in X         → version loaded but entity absent from it
        'Name' not found in any loaded version → entity not in the index at all
    """
    return _os.diff_entities(
        _os.get_client(), name, v1, v2, entity_type, allow_cross_source
    )
