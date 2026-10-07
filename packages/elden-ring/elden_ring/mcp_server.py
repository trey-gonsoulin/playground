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
    collapse_variants: bool = False,
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
            Matches the in-game effect / info lines too (effect_text, info_text,
            EN + JP; #90), e.g. "boosts Crystalian sorcery", "found near churches",
            and an Ash of War's "Usable on …" line (#169).
        entity_type: Narrow to one category. Loaded types (all first-party native
            extraction from the game's own params/FMGs):
            weapon       — all weapons with stats, scaling, and requirements;
                           effect / effects decode passives while held (on-kill
                           heals, catalyst spell-school boosts, regen; #163)
            armor        — all armor pieces with weight, defense, and negation values
            spell        — sorceries and incantations with FP cost, requirements and
                           decoded effects (buffs, heals, on-hit buildup; #88)
            ash_of_war   — weapon skills / ashes of war with effect descriptions
            item         — talismans (with SpEffect-derived effect / effects)
            ammo         — arrows, greatarrows, bolts, and ballista bolts (attack
                           by element, status_buildup / status_effects, and the
                           shot's projectile flight + follow-up hits and their
                           motion values; #91, #152)
            consumable   — usable items (throwables, buffs, online/multiplayer tools);
                           effect / effects decode what using one does (#88)
            key_item     — quest / story items (bell bearings, letters, whetblades, …)
            crafting_material — crafting ingredients (meat, fluids, plants, …)
            upgrade_material  — smithing stones, somber stones, golden seeds, sacred tears
            crystal_tear — Wondrous Physick crystal tears
            spirit_ash   — summonable spirit ashes (base row per summon)
            remembrance  — boss remembrances traded at the Roundtable Hold
            great_rune   — shardbearer Great Runes; effect / effects decode the
                           blessing, each conditioned 'requires Rune Arc' (#156)
            tool         — reusable crafting tools (cracked/ritual pots, perfume bottles)
            info         — informational items (letters, notes, memos)
            merchant     — NPC vendor inventories with item names and rune prices;
                           search by item name to find who sells it, or by merchant
                           name / location to get their full stock
            npc_dialogue — individual spoken lines from the TalkMsg text (searchable by quote);
                           cutscene subtitle lines name their scene in cutscene /
                           cutscene_id
            game_text    — other in-game text lines (#98), one doc per FMG id ("Loading
                           Tip 4", "Map Event 80810"); tags give the kind:
                           action_button (interaction prompts: "Touch grace"),
                           map_event (area/event banners: "Summoned Blaidd…"),
                           tutorial and loading_tip (display_name = title;
                           a tutorial's unlock_flag + unlock_set_when, #202),
                           item_dialog (item-use confirmations: "Use Stonesword
                           Key?"; #90)
            enemy      — bosses, creatures, and named enemies (from the NpcName roster)
                           with EN + JP names (name_ja); most also carry NpcParam
                           HP/stamina/poise/elemental defenses, status resistances +
                           immune_to, and traits (dragon, undead, …; #84) as base
                           values; stats_scaled groups the in-game (area-scaled)
                           hp / stamina / defense / resistances ranges over its
                           placements and, under ng_plus, the same stats
                           for NG+1..NG+7 (#108, #131, #261), plus an
                           attacks profile (elements, damage types, status effects
                           of the moves its animations fire; shared_with for
                           model-family move tables; #81, #123) and a
                           grabs profile for enemies that can grab you (#82),
                           critical_hits (backstab / riposte / stance_break /
                           downed / sleep flags; #128, #240), a
                           team_type (+ team label; same value = allies) and an ai
                           perception profile (sight/smell/leash ranges; #83). Enemies
                           have no in-game description. Bosses / named enemies carry a
                           drops list (items they drop, from map EMEVD scripts, #68, incl.
                           boss rewards such as remembrances and great runes, #134); the
                           dropped item's own doc carries the reciprocal dropped_by.
                           boss_encounters names the boss docs where it is fought;
                           summonable_for the bosses it can be summoned against, and
                           hostile_signs its NPC invasions / red-sign duels (#93).
                           Humanoid NPCs/invaders carry equipment (weapons, ashes of
                           war, armor, spells, talismans, ammo; #85); items carry the
                           reciprocal equipped_by. Their weapon_attacks (#127) list
                           each loadout weapon's level, attack power, status
                           buildup and poise damage, and spell_attacks (#267) each
                           loadout spell's base power, statuses and poise
                           (they usually have no `attacks`).
                           placements lists where each
                           instance stands (map + world_position in the open world,
                           else map-local position; #76) and maps the distinct maps;
                           regions / locations name the map-menu regions (+ tabs)
                           and dungeon location docs they fall in (#140); areas the
                           game's own region-map areas of the open-world ones (#194).
                           Generic mobs are named per model from their spirit ash
                           (Godrick Soldier, Demi-Human, …; name_source="spirit_ash",
                           chr_models) — model-level labels, not individual characters.
            boss         — one doc per boss encounter (#79, GameAreaParam): enemies
                           fought (phases, duo partners), location / region /
                           nearest_grace / map, runes, the defeat banner (boss tier:
                           Enemy Felled … Demigod Felled, Legend Felled, God Slain),
                           the items that encounter awards, and npc_summons (the NPC
                           summon signs for the fight + their quest gate flag;
                           #93, and requires_step, the quest step behind it:
                           {quest, phase_flag, order}; #189; else
                           requires_set_when, what turns the flag on, #228). Named after the
                           defeated character; a name several encounters share gets
                           the place appended, e.g. "Night's Cavalry (Gate Town
                           Bridge)" — search entity_type="boss" by name to list them.
                           cutscenes lists the encounter's cutscene docs ({id, kind}).
            cutscene     — one doc per realtime cutscene scene (#92), named
                           "Cutscene <id>": trigger_kind (boss_intro, boss_defeat,
                           ending, item, quest, scripted), the boss it belongs to,
                           map, trigger_flags (+ trigger_steps, the quest steps
                           behind them, #189; trigger_set_when, what turns the
                           other flags on, #228) / trigger_items (e.g. the Dectus medallion
                           halves), is_ending / unskippable, and its subtitles (EN +
                           subtitles_ja, in playback order; talk_ids join the
                           npc_dialogue lines). label is native (boss + kind, or the
                           first line); there are no hand-written scene names or
                           speakers. Search a quote to find the scene it's spoken in.
            site_of_grace — one doc per named Site of Grace (#78, BonfireWarpParam):
                           region + parent_region (the map-menu grouping), map,
                           position (map-local) or world_position (open world),
                           unlock_flag, and bosses (boss docs whose nearest grace it
                           is). A name two graces share gets its region appended,
                           e.g. "Elden Throne (Leyndell, Ashen Capital)".
            location     — one doc per named place (#77): the map menu's regions
                           (kind region: Limgrave, Caelid; subregion: Stormhill) —
                           the same values as region / parent_region elsewhere —
                           and world-map markers (kind catacombs, cave, ruins,
                           church, castle, legacy_dungeon, evergaol, …) with map,
                           position / world_position and region. graces and bosses
                           list what's inside; area_scaling gives the enemy scaling
                           tiers there (hp/stamina/attack/defense/resistance
                           multipliers, most common first). A repeated marker name
                           gets its region appended: "Minor Erdtree (Caelid)".
                           To list what's in a place, search enemy / item docs with
                           search_entities_literal(pattern="Caelid",
                           fields=["regions"]) or fields=["locations"] (a dungeon);
                           add "drop_regions" / "drop_locations" to include items
                           that enemies there drop. Graces and locations also carry
                           warps_to / warps_from (the places warps connect them to).
            warp         — one doc per scripted map-to-map warp (#94, EMEVD): kind
                           waygate / return_to_entrance / evergaol / cutscene /
                           scripted, from and to ({map, grace, region, location,
                           entity_id, position}), prompt ("Travel to another
                           location?"), gate_flag (waygates that need a flag;
                           gate_set_when, what turns it on, #228),
                           cutscene (the cutscene doc a kind-cutscene warp plays).
                           Named "from → to" by nearest grace, e.g. "The Four
                           Belfries → Dragon Temple"; evergaols "Stormhill Evergaol
                           (enter)"; a warp with no placed trigger "<to> (<kind>)".
            quest        — one doc per NPC questline (#95): the NPC's 20-flag event
                           block (flag_block; slots +0..+4 life state, +5..+19
                           phases; an NPC's second block with no death or
                           hostility evidence is all phases) read from the
                           event scripts. steps are phase
                           transitions (phase_flag, order, entered_from, the
                           locations whose scripts check the phase, and when: the
                           conditions — boss_defeated, talk, invasion, item_pickup,
                           item_held, another NPC's quest_phase / life_state,
                           hit_count (the NPC hit N times), a raw flag named only by the maps that set it, or any_of
                           with nested conditions, one of which holds). outcomes
                           are life-state changes (hostile / dead). Phases are
                           positional (order + location), not labelled; e.g.
                           search entity_type="quest", query="Millicent".
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
            Dagger, …) is its own name — see collapse_variants. Fuzzy matching makes
            this a relevance count, not an exact-phrase count; for corpus counts use
            search_entities_literal.
        source: If provided, restrict to documents from one internal game-data
            origin — the param table, FMG or script the docs were extracted from
            (e.g. EquipParamWeapon, EquipParamGoods, Magic, NpcName, GameAreaParam,
            EMEVD, TalkMsg). One source can back several entity_types
            (EquipParamGoods → consumable/key_item/…; EquipParamWeapon → weapon/ammo;
            EMEVD → warp/cutscene/quest), and one entity_type can come from several
            sources (game_text, location, enemy), so entity_type is usually the
            better filter. An unrecognized value returns {"error": ...} listing the
            live sources, and the retired "erdb"/"fextralife" say they were retired;
            describe_fields() has the live list.
        include_unavailable: By default, content that exists in the game data but is not
            obtainable is excluded from results — availability="cut" (name row [ERROR]-marked,
            e.g. Millicent's armor set) or availability="unobtainable" (real-named armor with
            no acquisition path, e.g. the Ragged set / enemy-only gear; #71). Pass True to
            include them; they carry the availability field so you can tell them apart from
            live content.
        collapse_variants: If True, keep only the base of each item variant family:
            drop docs with a base_item (weapon affinities like Heavy/Keen/…/Occult,
            talisman ranks +1/+2/+3, flask +N, altered armor), which are separate docs
            with their own names and often copies of the base text. Everything outside
            a family is unaffected. Use it to count distinct items.
        collapse_affinity: Deprecated alias for collapse_variants.

    Returns a list of entity documents when count_only is False, each with at minimum:
    entity_type, name, patch_version and source; description is present on items,
    equipment, dialogue, game_text and merchants but not on enemy, boss, location,
    site_of_grace, warp, quest or cutscene docs. Use get_entity() for the
    full document of a specific named entity, or describe_fields() to see all
    queryable fields. Item documents may include cross-reference edge fields:
      sold_by            — merchant names that sell this item (per-patch)
      shop_listings      — per shop row: vendor/price/currency/quantity/unlock_flag (#89;
                           get_entity only, not searchable), requires_dlc (the paid
                           DLC gating the row, e.g. "Tarnished Pack", #210), unlocked_by /
                           unlocked_by_defeating (the goods / bosses that unlock it, #144),
                           handed_to (the NPC the goods are given to); resold_from /
                           resale_flags on Twin Maiden Husks re-sales (bell bearing, #145)
      dropped_by         — boss / named enemies that drop this item (EMEVD-derived, #68)
      equipped_by        — humanoid enemies/NPCs whose loadout includes it (#85)
      given_by           — NPCs whose dialogue gives this item (talk scripts, #23)
      in_exchange_for    — the item the giving NPC takes for it (turn-in, #97);
                           exchanged_for is the reverse, on the handed-in item;
                           in_exchange_count = the n-th hand-in (Gurranq's Deathroot, #193);
                           in_exchange_flag / in_exchange_step = the flag that hand-in
                           also needs on (his aggression event 3647 for the 5th–9th, #200)
      duplication        — a duplication menu for an item you hold, not a sale (#223):
                           {service, where, also_at, only_at_bell_mausoleums, price,
                           currency, quantity, unlock_flag, unlocked_by_defeating}; Ashes
                           of War at Smithing Master Hewg (1 Lost Ashes of War),
                           remembrances at a Wandering Mausoleum (free, once; also_at the
                           DLC Stone Coffin Altars, 1.12+; demigods' only at the bell ones)
      starting_classes   — starting classes whose initial loadout includes it (#23)
      acquisition_types  — how it's obtained: merchant / enemy_drop / found_in_world /
                           chest / corpse (looted from a body, #136) /
                           given_by_npc / starting_equipment / keepsake / crafted /
                           gathered (gathering-node pickup, #142) /
                           quest_reward (scripted quest award, #137) /
                           invader_drop (defeating an NPC invader, #138) /
                           altered ((Altered) armor made from its base piece, #224) /
                           interaction_reward (awarded on an interaction alone: a
                           painting taken off its wall, a restored Great Rune, a DLC
                           Ruined Forge furnace, #147)
      crafted_from       — crafting recipe materials [{item, quantity}], with
                           crafted_yield and recipe_unlock (cookbooks) (#87)
      used_in / unlocks_recipes — on materials / cookbooks: the items they craft (#87)
      unlocks_shop_items — on bell bearings / scrolls / bosses: items whose shop rows
                           they unlock (#144)
      placements / maps  — where it is picked up in the world (MSB treasure: map,
                           world_position or map-local position, in_chest; #76;
                           on_corpse when looted from a body, #136;
                           gathering nodes flagged gathering: true, #135)
      regions / locations — map-menu regions (+ tabs) and dungeon location docs of
                           those placements (#140)
      areas              — the game's region-map areas of those placements (#194)
      drop_regions / drop_locations — the same, for the dropped_by enemies'
                           placements that carry the item (#141)
    Item variants (weapon affinities, talisman ranks, flask +N, altered armor) are their
    own docs and link to their base via base_item; the base doc's variants field
    summarizes the family. Talisman ranks also carry text_differs: True when their text
    diverges from the base beyond the effect-magnitude rewording every rank has ("Boosts" → "Greatly boosts" and
    上昇 → 大きく上昇 are ignored). text_added_lines lists the new lines, e.g.
    「伝説のタリスマン」のひとつ. False means only the magnitude wording changed.

    Returns {"total": N} when count_only is True, and {"error": ...} listing the
    valid values for an unknown or retired entity_type / source / patch_version
    or an unmapped include_fields name (not an empty list).
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
        collapse_variants,
    )


@mcp.tool(annotations=_READ_ONLY)
def get_entity(
    name: str,
    entity_type: str | None = None,
    include_variants: bool = False,
    include_placements: bool = False,
    patch_version: str | None = None,
) -> dict | None:
    """Retrieve the full data document for a named Elden Ring entity.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        name: Exact entity name (case-sensitive), e.g. "Rivers of Blood".
        entity_type: Optional type hint to disambiguate if two entities share
            a name across categories (e.g. a boss and its enemy doc).
        include_variants: If True, add variant_docs: the full docs of the rest of the
            entity's item variant family at the same patch, in sort order (the base
            plus every doc naming it in base_item). Works from the base (Halberd ->
            its 12 affinities) or a variant (Heavy Halberd -> Halberd + the other
            affinities); an entity outside a family gets an empty list.
        include_placements: If True, return every entry of placements even when
            there are more than 50 (see below).
        patch_version: Return the document as of this patch (e.g. "1.10.0" for the
            base game before the DLC) instead of the newest. Resolved as-of the
            entity type's own loaded versions: the latest one at or before it, so
            npc_dialogue (loaded once per Data0 group) works at any patch, and the
            result then carries requested_patch_version alongside the doc's own
            patch_version. Returns {"error": ...} if the version isn't loaded (see
            list_patch_versions()) or the entity didn't exist yet (a DLC item at
            1.10.0). include_variants uses the same patch.

    Named variants (weapon affinities, talisman ranks, flask +N, altered armor) are
    separate docs linked by base_item, and the base doc's variants field summarizes
    them (name, affinity or rank, which fields differ, compact values). Enemy
    variants that share one display name (Rennala's two phases) are nested in the
    enemy doc's variants instead. A multi-phase boss doc (Maliketh, Malenia, Elden
    Beast, Hoarah Loux…) carries the whole fight in phases: each phase's character,
    its own stats and HP, and the HP ratio its phase ends at. A boss and its enemy
    share a name ("Godrick the Grafted"): pass entity_type="boss" for the encounter
    doc (arena, runes, banner), "enemy" for the character's stats, and
    "site_of_grace" for a grace named after its boss.

    Historical names resolve too: an item renamed across patches is indexed under
    its current name, with the per-patch name kept in display_name. Looking one up
    by an old name (e.g. "Celebrant's Flame Art Cleaver Blades") returns the
    newest document for the current name, with name_is_historical=true and
    queried_name set to your input. Pass patch_version for the old patch's doc
    (with that patch's display_name), or use diff_entities to compare.

    placements (MSB world positions) are returned only when there are at most 50;
    a longer list (common gathering materials have thousands of nodes, busy enemy
    types hundreds) is replaced by placements_total, and maps / regions / areas /
    locations still say where. Pass include_placements=True for the full list.

    Returns the full document dict, or null if the entity is not in the index
    ({"error": ...} for an entity_type that isn't loaded).
    """
    doc = _os.get_entity(
        _os.get_client(), name, entity_type, include_variants, patch_version
    )
    return doc if include_placements else _os.trim_placements(doc)


@mcp.tool(annotations=_READ_ONLY)
def calculate_attack_rating(
    weapon: str,
    str: int = 10,
    dex: int = 10,
    int: int = 10,
    fai: int = 10,
    arc: int = 10,
    level: int | None = None,
    two_handed: bool = False,
    affinity: str | None = None,
    patch_version: str | None = None,
) -> dict:
    """Compute a weapon's attack rating and status buildup for given character stats.

    Stored weapon stats (attack_power, scaling, status_buildup) ignore the character;
    this tool applies the stats the way the game does:
    - each damage type = base x (1 + sum of scaling x stat curve) for the stats
      that scale it (AttackElementCorrectParam + CalcCorrectGraph);
    - a requirement not met (after the two-handing bonus) makes every type that
      scales with that stat deal base x 0.6, listed in penalized;
    - two-handing counts Str as floor(Str x 1.5), except on paired weapons; bows
      and ballistae are always two-handed;
    - poison, bleed, sleep and madness buildup scale with Arcane (not two-handed
      Str); rot, frost and death blight don't scale;
    - staves and seals also return spell_scaling per damage type;
    - thrown consumables (Fire Pot, Kukri, Poisonbone Dart) scale their flat hit
      power and buildup the same way through a hidden weapon row, at level 0 with
      no requirements (Fire Pot 230 fire -> 284 at 10 Str / 10 Dex).
    Values are floored per type like the in-game menu. Not modeled: buffs,
    talismans, great runes, enemy defense.

    If this tool returns a connection error, call start_search_service() first.

    Args:
        weapon: Weapon name, e.g. "Uchigatana" or "Blood Uchigatana", or a thrown
            consumable, e.g. "Fire Pot".
        str, dex, int, fai, arc: Character attributes, 1-99 (default 10).
        level: Upgrade level; defaults to the weapon's max (+25, somber +10).
        two_handed: Wield two-handed (Str x 1.5). Ignored for consumables, which
            aren't wielded (the result then carries a note saying so).
        affinity: Optional affinity prefix for infusable weapons, e.g. "Heavy",
            "Blood", "Occult"; same as passing "Heavy Halberd" as weapon.
        patch_version: Compute with that patch's data (e.g. "1.07.0", resolved to
            the latest loaded patch at or before it); default newest.

    Returns a dict with:
        weapon, patch_version, level, max_level, two_handed, stats, requirements
        effective_stats: stats after the two-handing bonus
        attack_power: {type: {base, scaling, total}} per damage type
        total: sum of the attack_power totals (the menu's attack rating)
        status_buildup: {status: {base, scaling, total}} for on-hit buildup
        spell_scaling: {type: value} for staves and seals, else null
        unmet_requirements: stats below requirement, else null
        penalized: types dealt at x0.6 for an unmet requirement, else null
    Or {"error": "..."} for an unknown weapon, bad level/stats, or a patch
    without the data.
    """
    return _os.calculate_attack_rating(
        _os.get_client(),
        weapon,
        {"str": str, "dex": dex, "int": int, "fai": fai, "arc": arc},
        level,
        two_handed,
        affinity,
        patch_version,
    )


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
        source: If provided, restrict counts to documents extracted from one game
            param table (e.g. "EquipParamWeapon", "EquipParamProtector",
            "EquipParamGoods"). All data is first-party native extraction, so there
            are no cross-source duplicates to exclude, and entity_type is usually the
            better filter. An unrecognized or retired value returns {"error": ...}
            listing the live sources (also in describe_fields()). There is no patch filter:
            counts are distinct names across every loaded patch, DLC included.

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
    infusable, default_ash_of_war, damage_types, is_legendary, depicts_weapon, and the
    .ja/.morph/.lemma Japanese subfields. Stats are grouped objects listed by dotted
    path (attack_power.fire, attack_power.critical, scaling.str.grade,
    scaling.str.value, requirements.dex, guard.physical, guard.resistances.bleed,
    status_buildup.bleed, max_level.attack_power.physical, max_level.scaling.str.grade, negation.slash,
    stats.hp, defense.fire, resistances.bleed, summon_stats.stats.hp). Weapons at max
    upgrade are under max_level; the per-level upgrade_curve comes back from
    get_entity. Spirit ashes carry their summons' +0 stats as summon_stats, +10 as
    max_level.summon_stats, and every level in upgrade_curve.summon_stats. Both carry
    each level's upgrade cost in upgrade_curve.materials (#87) and
    upgrade_curve.rune_cost (#143). Weapons
    carry per-attack poise damage (poise_damage.two_handed.charged_r2, running,
    rolling, crouch and jumping attacks; one-handed powerstance, left-hand and
    mounted attacks such as poise_damage.one_handed.mounted_charged_r2; PvP under
    poise_damage.pvp); full hit chains come back from get_entity as poise_damage_chains.
    Weapons also carry their default skill's per-hit poise (skill_poise_damage.max,
    .hits, .pvp; #125), each hit labeled in .hit_labels (FP / no FP, light / heavy;
    #167); projectile hits in .projectile_hits with .projectile_hit_counts (times
    each lands per use, #168) and .projectile_hit_labels (#171). Ammo carries
    its bow-skill shots (Mighty Shot, Barrage, Rain of Arrows...) in skill_shots
    (#151): per skill and hit, motion_values, poise_damage, hit_count, projectile.
    Bows and bow Ashes of War carry their skill's skill_shots with the standard
    ammo, named in skill_shots.ammo (#177). Enemy and summon attack profiles carry
    the max per-hit poise damage as attacks.poise_damage (#253); on
    summon_stats.attacks it includes every spirit's x0.05.

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
        versions: list[str] — patch versions sorted oldest-first (semantic order).
                  1.02–1.06 are labeled without a third part ("1.02") and 1.07.0
                  on with one; every tool accepts either spelling ("1.02.0",
                  "1.10") and maps it to the loaded label.
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
    collapse_variants: bool = False,
) -> dict:
    """Search for entities containing an exact literal substring across text fields.

    Uses phrase matching rather than fuzzy relevance ranking, so the pattern must
    appear verbatim in the text. Suited for corpus-wide morpheme and construction
    tracking where exact counts matter more than relevance ordering.

    Omit pattern (or pass None) to enumerate all entities matching other filters
    without a text constraint — useful for structural queries like "all distinct
    weapons" via collapse_variants, or census queries via count_only.

    Counting: every infusable weapon is indexed once per affinity (Raptor Talons, Heavy
    Raptor Talons, … 13 docs), and the variants carry the base's text, so a Japanese
    phrase in one armament's description counts up to 13 times. Pass
    collapse_variants=True to count distinct items: Raptor Talons then counts once
    for 凶手 instead of 13 times. Don't use sort_id_mod=1000 for this: it drops
    every non-weapon match and every armament whose sort_id isn't a multiple of
    1000 — base game included (Butchering Knife 2106500, Prelate's Inferno Crozier
    2504500), and many DLC armaments (+100 … +900 offsets).

    Unknown arguments return {"error": ...} rather than a silent 0: a misspelled
    or retired source, an entity_type or patch_version that isn't loaded, or a
    field / include_fields name that isn't in the mapping; the error lists the
    valid values. A field that exists but that no doc of the filtered entity type
    carries (text_content_ja on weapons) returns its 0 with "warnings" naming the
    text fields that type does carry.

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
        fields: Which fields to search. Defaults to the eleven text fields: name,
            display_name, description, text_content, effect_text, info_text, name_ja,
            description_ja, text_content_ja, effect_text_ja, info_text_ja (the
            effect/info lines are #90). Not every type carries every field:
            equipment (weapon, armor, spell, item, ash_of_war, ammo) has its JP flavor
            text only in description_ja and no text_content / text_content_ja;
            npc_dialogue, game_text and merchant have JP text only in
            text_content_ja. Naming a field the type lacks returns 0 plus a
            "warnings" entry — see the per-field notes in describe_fields(). Japanese fields named here are routed to the subfield for
            the active mode (.morph when use_kuromoji=True, .lemma when
            use_lemmatize=True), so an explicit fields list composes correctly with
            those modes. The acquisition keyword fields (sold_by, acquisition_sources,
            acquisition_types) are NOT in the default set — to search them (e.g. find
            everything a merchant sells) you must name them explicitly, e.g.
            fields=["sold_by"]. See describe_fields() for the full field catalog.
        entity_type: Narrow to one entity category (weapon, armor, spell, consumable,
            key_item, spirit_ash, ammo, enemy, boss, site_of_grace, etc.). See search_entities() for the full
            list or call list_entity_types() for the authoritative live set.
        patch_version: Filter to a specific patch snapshot (e.g. "1.10.0"). Omit to
            search across all patches and return one result per entity (latest version).
            Use list_patch_versions() to see available versions. Trailing-zero
            spellings are aliases ("1.02.0" → "1.02", "1.10" → "1.10.0"); a remapped
            request returns requested_patch_version and the patch_version used.
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
            (enemies, merchants, dialogue) never match. Not a way to count distinct
            armaments: some bases aren't 1000-aligned in the base game too (Butchering
            Knife 2106500, Prelate's Inferno Crozier 2504500) and many in the DLC, so
            it silently drops them. Use collapse_variants instead.
        sort_id_remainder: Remainder for the modulo filter (default 0).
        use_kuromoji: If True, route Japanese fields through kuromoji morpheme segmentation
            so phrase queries respect dictionary word boundaries. Prevents single-kanji
            queries from matching compounds that contain that kanji as a sub-character.
            Has no effect on English fields. Default False (standard CJK-unigram mode).
            See the suru-verb caveat above before trusting zero results from this mode.
        source: If provided, restrict to documents from one internal game-data origin
            (the param/FMG/script the docs were extracted from, e.g. "EquipParamWeapon",
            "Magic", "TalkMsg", "EMEVD"). Not 1:1 with entity_type: EquipParamGoods
            backs ten goods types, EMEVD backs warp/cutscene/quest, and game_text and
            location each come from several. An unrecognized or retired value
            ("erdb") returns {"error": ...}; describe_fields() has the live list. Prefer entity_type unless you want the game-structure view.
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
        collapse_variants: If True, keep only the base of each item variant family
            (drop docs with a base_item: weapon affinities, talisman ranks, flask +N,
            altered armor). Everything outside a family is unaffected. Also applies to
            total.
        collapse_affinity: Deprecated alias for collapse_variants.

    Returns a dict with:
        total: int — the full match count, never capped by limit (results may be
            shorter; total is still exact).
            - Without patch_version: the number of distinct names across all patches
              (exact below 40,000). A name shared by two entity_types counts once;
              each weapon affinity counts separately unless collapse_variants=True.
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
        collapse_variants,
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
        entity_type: Entity category to scan (any list_entity_types() value, e.g.
            weapon, armor, spell, item, ash_of_war, merchant, npc_dialogue, game_text).
        field: Field to compare, e.g. "description", "description_ja", "text_content",
            "location", "effect", "display_name" (per-patch FMG name — use this to find
            weapons renamed across patches). Any indexed field works, including a grouped
            stat by dotted path ("attack_power.physical", "stats.hp"); missing values
            compare as null. Call describe_fields() for the full list.
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
        changed_fields: {field: {v1: old_value, v2: new_value}} for each changed field;
            grouped stats are compared per leaf by dotted path (attack_power.physical)
        unchanged_fields: [field, ...] for fields present in both with identical values

    Returns {"error": "..."} if either version is not loaded or the entity is not
    present in the requested versions.
    Error cases:
        patch version 'X' is not loaded → version not in the index
        'Name' not present in X         → version loaded but entity absent from it
        'Name' not found in any loaded version → entity not in the index at all
    """
    return _os.diff_entities(_os.get_client(), name, v1, v2, entity_type)
