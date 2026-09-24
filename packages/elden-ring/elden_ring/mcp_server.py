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
    include_unavailable: bool = False,
    collapse_affinity: bool = False,
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
        entity_type: Narrow to one category. Loaded types (all first-party native
            extraction from the game's own params/FMGs):
            weapon       — all weapons with stats, scaling, and requirements
            armor        — all armor pieces with weight, defense, and negation values
            spell        — sorceries and incantations with FP cost and requirements
            ash_of_war   — weapon skills / ashes of war with effect descriptions
            item         — talismans (with SpEffect-derived effect text) and equippables
            ammo         — arrows, greatarrows, bolts, and ballista bolts
            consumable   — usable items (throwables, buffs, online/multiplayer tools)
            key_item     — quest / story items (bell bearings, letters, whetblades, …)
            crafting_material — crafting ingredients (meat, fluids, plants, …)
            upgrade_material  — smithing stones, somber stones, golden seeds, sacred tears
            crystal_tear — Wondrous Physick crystal tears
            spirit_ash   — summonable spirit ashes (base row per summon)
            remembrance  — boss remembrances traded at the Roundtable Hold
            great_rune   — shardbearer Great Runes
            tool         — reusable crafting tools (cracked/ritual pots, perfume bottles)
            info         — informational items (letters, notes, memos)
            merchant     — NPC vendor inventories with item names and rune prices;
                           search by item name to find who sells it, or by merchant
                           name / location to get their full stock
            npc_dialogue — individual spoken lines from the TalkMsg text (searchable by quote)
            enemy        — bosses, creatures, and named enemies (from the NpcName roster)
                           with EN + JP names (name_ja); the humanoid subset also carries
                           HP/stamina/poise/elemental-defense stats from NpcParam. Enemies
                           have no in-game description. Bosses / named enemies carry a
                           drops list (items they drop, from map EMEVD scripts, #68); the
                           dropped item's own doc carries the reciprocal dropped_by.
            Call list_entity_types() for the authoritative live list.
        patch_version: Filter to a specific game patch (e.g. "1.07.0"). Native data
            is extracted per-patch from that patch's regulation.bin, so this is the
            real in-game version, not a scrape snapshot — trustworthy for tracking
            when stats/text changed. Use list_patch_versions() to see what's loaded.
            Omit to return one result per entity (latest indexed version); pass
            a specific version to scope results to that snapshot only.
        limit: Maximum results to return (default 20, max 100). The list is capped here
            and carries no total — use count_only for a count.
        include_fields: If provided, only these fields are returned per document (e.g.
            ["name", "sort_id"]). Reduces payload size for large result sets.
        count_only: If True, return {"total": N} instead of the full document list.
            N is never capped by limit. Without patch_version it is the number of
            distinct names (a name shared by two entity_types counts once); with
            patch_version it is the number of matching docs in that snapshot, which is
            one per (entity_type, name). Each weapon affinity (Heavy Dagger, Keen
            Dagger, …) is its own name — see collapse_affinity. Fuzzy matching makes
            this a relevance count, not an exact-phrase count; for corpus counts use
            search_entities_literal.
        source: If provided, restrict to documents from one internal game-data
            origin — the param table or FMG the docs were extracted from:
            EquipParamWeapon, EquipParamProtector, Magic, EquipParamAccessory,
            EquipParamGem, EquipParamGoods, ShopLineupParam, TalkMsg. One source can
            back several entity_types (EquipParamGoods → consumable/key_item/…;
            EquipParamWeapon → weapon/ammo), so entity_type is usually the better
            filter; use source when you specifically want to think in terms of the
            underlying game structure. Call describe_fields() for the live list.
        include_unavailable: By default, content that exists in the game data but is not
            obtainable is excluded from results — availability="cut" (name row [ERROR]-marked,
            e.g. Millicent's armor set) or availability="unobtainable" (real-named armor with
            no acquisition path, e.g. the Ragged set / enemy-only gear; #71). Pass True to
            include them; they carry the availability field so you can tell them apart from
            live content.
        collapse_affinity: If True, keep only the Standard row of each infusable weapon
            (drop Heavy/Keen/…/Occult variants, which are separate docs with their own
            names and copies of the base text). Non-weapons are unaffected. Use it to
            count distinct armaments.

    Returns a list of entity documents when count_only is False, each with at minimum:
    entity_type, name, patch_version, source, description. Use get_entity() for the
    full document of a specific named entity, or describe_fields() to see all
    queryable fields. Item documents may include cross-reference edge fields:
      sold_by            — merchant names that sell this item (per-patch)
      dropped_by         — boss / named enemies that drop this item (EMEVD-derived, #68)
      acquisition_types  — how it's obtained: merchant / enemy_drop / found_in_world
    Talisman rank variants (e.g. Erdtree's Favor +2) link to their base via base_item and
    carry text_differs: True when their text diverges from the base beyond the
    effect-magnitude rewording every rank has ("Boosts" → "Greatly boosts" and
    上昇 → 大きく上昇 are ignored). text_added_lines lists the new lines, e.g.
    「伝説のタリスマン」のひとつ. False means only the magnitude wording changed.

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
        include_unavailable,
        collapse_affinity,
    )


@mcp.tool(annotations=_READ_ONLY)
def get_entity(name: str, entity_type: str | None = None) -> dict | None:
    """Retrieve the full data document for a named Elden Ring entity.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        name: Exact entity name (case-sensitive), e.g. "Rivers of Blood".
        entity_type: Optional type hint to disambiguate if two entities share
            a name across categories (e.g. a boss and a lore entry).

    Historical names resolve too: an item renamed across patches is indexed under
    its current name, with the per-patch name kept in display_name. Looking one up
    by an old name (e.g. "Celebrant's Flame Art Cleaver Blades") returns the
    newest document for the current name, with name_is_historical=true and
    queried_name set to your input. Use diff_entities to see the old patch's values.

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
def describe_fields() -> dict:
    """Describe what is queryable in the index: entity types, sources, and fields.

    Use this to discover the schema instead of guessing field names. Every field
    that can be searched (search_entities_literal fields=), filtered (source=), or
    diffed (text_changed_between field=) is listed with its type and a note where
    the meaning isn't obvious — including the native-rich fields like effect,
    negation_slash/strike/pierce, infusable, default_ash_of_war, is_legendary,
    depicts_weapon, and the .ja/.morph/.lemma Japanese subfields.

    If this tool returns a connection error, call start_search_service() first.

    Returns:
        entity_types: {type: doc_count} — loaded categories with document counts
        sources:      {source: doc_count} — internal game-data origins (param/FMG
                      names the docs were extracted from)
        fields:       {field: {type, subfields?, note?}} — the queryable field catalog
    """
    return _os.describe_index(_os.get_client())


@mcp.tool(annotations=_READ_ONLY)
def list_patch_versions() -> dict:
    """List the game patch versions currently loaded in the index.

    All data is first-party native extraction, one snapshot per game patch, so
    each version string is a real in-game patch. Use them as v1/v2 arguments to
    diff_entities()/text_changed_between(), or as the patch_version filter in
    search_entities(). For the internal data-origin values (params/FMGs) and the
    full field catalog, call describe_fields().

    If this tool returns a connection error, call start_search_service() first.

    Returns:
        versions: list[str] — patch versions sorted oldest-first (semantic order)
    """
    return {"versions": _os.list_patch_versions(_os.get_client())}


@mcp.tool(annotations=_READ_ONLY)
def analyze_text(text: str) -> dict:
    """Return the token stream for a text string under all three indexed JP analyzers.

    Calls OpenSearch's _analyze API using the exact field paths that search_literal()
    applies, so the output reflects precisely what a phrase query will match:
    - standard: CJK unigram tokenization (default mode, use_kuromoji=False)
    - kuromoji_segmenter: dictionary segmentation, no lemmatization (use_kuromoji=True)
    - kuromoji_lemmatizer: dictionary segmentation + baseform reduction (use_lemmatize=True)

    Use this to diagnose unexpected zeros before concluding a morpheme is absent.
    In particular, single-kanji suru-verbs that lack IPADIC entries (e.g. 模す, 象る)
    may be split differently than expected — see the use_kuromoji docstring on
    search_entities_literal for details. Also use this to verify what baseform a verb
    reduces to before using use_lemmatize=True.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        text: Any string to analyze, e.g. "模した", "象る", "Eternal Dragon".

    Returns:
        standard: list[str] — tokens under standard CJK-unigram tokenization
        kuromoji_segmenter: list[str] — tokens under kuromoji segmentation-only mode
        kuromoji_lemmatizer: list[str] — tokens under kuromoji baseform reduction
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
    use_lemmatize: bool = False,
    include_unavailable: bool = False,
    collapse_affinity: bool = False,
) -> dict:
    """Search for entities containing an exact literal substring across text fields.

    Uses phrase matching rather than fuzzy relevance ranking, so the pattern must
    appear verbatim in the text. Suited for corpus-wide morpheme and construction
    tracking where exact counts matter more than relevance ordering.

    Omit pattern (or pass None) to enumerate all entities matching other filters
    without a text constraint — useful for structural queries like "all distinct
    weapons" via collapse_affinity, or census queries via count_only.

    Counting: every infusable weapon is indexed once per affinity (Raptor Talons, Heavy
    Raptor Talons, … 13 docs), and the variants carry the base's text, so a Japanese
    phrase in one armament's description counts up to 13 times. Pass
    collapse_affinity=True to count distinct armaments: Raptor Talons then counts once
    for 凶手 instead of 13 times. Don't use sort_id_mod=1000 for this — it also drops
    every non-weapon match and DLC bases (whose sort_ids aren't 1000-aligned).

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
        fields: Which fields to search. Defaults to the seven text fields: name,
            display_name, description, text_content, name_ja, description_ja,
            text_content_ja. Japanese fields named here are routed to the subfield for
            the active mode (.morph when use_kuromoji=True, .lemma when
            use_lemmatize=True), so an explicit fields list composes correctly with
            those modes. The acquisition keyword fields (sold_by, acquisition_sources,
            acquisition_types) are NOT in the default set — to search them (e.g. find
            everything a merchant sells) you must name them explicitly, e.g.
            fields=["sold_by"]. See describe_fields() for the full field catalog.
        entity_type: Narrow to one entity category (weapon, armor, spell, consumable,
            key_item, spirit_ash, ammo, enemy, etc.). See search_entities() for the full
            list or call list_entity_types() for the authoritative live set.
        patch_version: Filter to a specific patch snapshot (e.g. "1.10.0"). Omit to
            search across all patches and return one result per entity (latest version).
            Use list_patch_versions() to see available versions.
        limit: Maximum results to return (default 200, max 500).
        include_fields: If provided, only these fields are returned per document (e.g.
            ["name", "sort_id"]). Reduces payload when full documents aren't needed.
        count_only: If True, return {"total": N} without a results list. Useful for
            census queries (e.g. count all weapons of a given type) without fetching
            any documents. See Returns for exactly what N counts.
        sort_id_gte: Filter to entities with sort_id >= this value.
        sort_id_lte: Filter to entities with sort_id <= this value.
        sort_id_mod: If set, keep only entities where sort_id % sort_id_mod == sort_id_remainder.
            A raw structural filter on the in-game sort index: docs without a sort_id
            (enemies, merchants, dialogue) never match. To count distinct armaments use
            collapse_affinity instead (DLC weapon bases aren't 1000-aligned).
        sort_id_remainder: Remainder for the modulo filter (default 0).
        use_kuromoji: If True, route Japanese fields through kuromoji morpheme segmentation
            so phrase queries respect dictionary word boundaries. Prevents single-kanji
            queries from matching compounds that contain that kanji as a sub-character.
            Has no effect on English fields. Default False (standard CJK-unigram mode).
            See the suru-verb caveat above before trusting zero results from this mode.
        source: If provided, restrict to documents from one internal game-data origin
            (the param/FMG the docs were extracted from, e.g. "EquipParamWeapon",
            "Magic", "TalkMsg"). Maps 1:1 to entity_type today; call describe_fields()
            for the live list. Prefer entity_type unless you want the game-structure view.
        use_lemmatize: If True, route Japanese fields through kuromoji baseform reduction
            so a single query in dictionary form matches all inflected surface forms.
            Example: pattern="与える" matches docs containing 与えた, 与えられ, 与えて, etc.
            Uses segmentation + kuromoji_baseform only (no stopword/POS removal), so
            phrase queries across word boundaries work correctly. Takes precedence over
            use_kuromoji when both are True. Use analyze_text() to verify the expected
            baseform before querying — IPADIC-unknown verbs (e.g. 模す, 象る) may not
            reduce to the expected baseform.
        include_unavailable: By default, unavailable content (availability="cut" or
            "unobtainable", e.g. Millicent's set / the Ragged set) is excluded. Pass True to
            include it — useful for census/corpus queries that should count everything present
            in the game data. This also affects count_only totals.
        collapse_affinity: If True, keep only the Standard row of each infusable weapon
            (drop Heavy/Keen/…/Occult variants). Non-weapons are unaffected. Also
            applies to total.

    Returns a dict with:
        total: int — the full match count, never capped by limit (results may be
            shorter; total is still exact).
            - Without patch_version: the number of distinct names across all patches
              (exact below 40,000). A name shared by two entity_types counts once;
              each weapon affinity counts separately unless collapse_affinity=True.
            - With patch_version: the number of matching docs in that snapshot, which
              is one per (entity_type, name) — no cross-patch duplicates.
            - With patterns: the size of the de-duplicated union by name. It's exact
              unless a single pattern matches more than 10,000 docs, in which case
              it's a lower bound.
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
        use_lemmatize,
        include_unavailable,
        collapse_affinity,
    )


@mcp.tool(annotations=_READ_ONLY)
def text_changed_between(
    entity_type: str,
    field: str,
    v1: str,
    v2: str,
    count_only: bool = False,
) -> list[dict] | dict:
    """Find all entities of a type where a specific field changed between two patch versions.

    Runs two bulk queries (one per version) and compares at the application layer.
    Suited for corpus-wide analysis — e.g. "which talismans had their description
    rewritten between 1.02.1 and 1.10.0?". Two calls (one for description, one for
    description_ja) reveal whether changes are lore rewrites or localisation-only.
    Because all data is native per-patch extraction, differences reflect real game
    revisions rather than scrape artifacts.

    Only entities present in both versions are included. To check whether an entity
    was added or removed between patches, use diff_entities().

    Sparse entity types are handled by as-of resolution: npc_dialogue is indexed
    once per Data0 text group, so a requested version resolves to the latest group
    representative at or before it, making any patch pair comparable.

    Call list_patch_versions() first to see what's loaded.
    Call list_entity_types() to see valid entity_type values.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        entity_type: Entity category to scan (weapon, armor, spell, item, ash_of_war,
            merchant, npc_dialogue).
        field: Field to compare, e.g. "description", "description_ja", "text_content",
            "location", "effect", "display_name" (per-patch FMG name — use this to find
            weapons renamed across patches). Any indexed field works; missing values compare
            as null. Call describe_fields() for the full list.
        v1: Older patch version, e.g. "1.02.1".
        v2: Newer patch version, e.g. "1.10.0".
        count_only: If True, return {"total": N} instead of the full diff list. Useful
            for census queries without paying the cost of returning all before/after values.

    Returns a list of dicts, one per changed entity, each with:
        name: entity name
        text_before: field value at v1 (null if absent)
        text_after: field value at v2 (null if absent)
    Sorted alphabetically by name.

    Returns {"total": N} when count_only is True.
    Returns {"error": "..."} if either version is not loaded.
    """
    return _os.text_changed_between(
        _os.get_client(), entity_type, field, v1, v2, count_only
    )


@mcp.tool(annotations=_READ_ONLY)
def diff_entities(
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
) -> dict:
    """Compare an entity's fields between two patch versions.

    Returns which fields changed (with before/after values) and which were
    unchanged. Useful for tracking description rewrites, stat adjustments, or
    location changes between patches. All data is native per-patch extraction, so
    differences reflect real game revisions.

    Call list_patch_versions() first to see what's loaded.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        name: Exact entity name (case-sensitive), e.g. "Longtail Cat Talisman".
            A historical (pre-rename) name also works; it's resolved to the
            item's current name, so the diff spans the rename.
        v1: Older patch version, e.g. "1.06.0".
        v2: Newer patch version, e.g. "1.07.0".
        entity_type: Optional type hint to disambiguate if two entities share a name.

    Returns a dict with:
        name: the entity's current (canonical) name
        queried_name: your input, only when it differed from name
        changed: bool — whether any fields differ
        changed_fields: {field: {v1: old_value, v2: new_value}} for each changed field
        unchanged_fields: [field, ...] for fields present in both with identical values

    Returns {"error": "..."} if either version is not loaded or the entity is not
    present in the requested versions.
    Error cases:
        patch version 'X' is not loaded → version not in the index
        'Name' not present in X         → version loaded but entity absent from it
        'Name' not found in any loaded version → entity not in the index at all
    """
    return _os.diff_entities(_os.get_client(), name, v1, v2, entity_type)
