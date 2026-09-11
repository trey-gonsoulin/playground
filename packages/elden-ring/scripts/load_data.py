#!/usr/bin/env python3
"""
Elden Ring data loader — downloads pre-parsed game data and bulk-indexes it
into the OpenSearch cluster.

Primary source: EldenRingDatabase/erdb gamedata zips
  - EquipParamWeapon.csv  → weapons (stats, scaling, requirements)
  - EquipParamProtector.csv → armor (weight, defenses)
  - Magic.csv             → spells (FP cost, slots, stat requirements)
  - EquipParamGem.csv     → ashes of war
  - EquipParamAccessory.csv → talismans
  - *.fmg.xml             → in-game names and descriptions for all of the above

Patch version is inferred from the zip filename (e.g. 1.10.0) and stored on
every document so future reloads from newer patches can be distinguished.

Usage:
    # Set the endpoint (from sam deploy output or CloudFormation console):
    export OPENSEARCH_ENDPOINT=ec2-xx-xx-xx-xx.compute-1.amazonaws.com
    export OPENSEARCH_USER=admin
    export OPENSEARCH_PASSWORD=<your-password>

    uv run --package elden-ring python packages/elden-ring/scripts/load_data.py [--dry-run]

    # Or point at a specific erdb version:
    uv run --package elden-ring python packages/elden-ring/scripts/load_data.py --erdb-version 1.10.0
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict

import requests
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.helpers import bulk

# ---------------------------------------------------------------------------
# OpenSearch client
# ---------------------------------------------------------------------------

def _get_client() -> OpenSearch:
    endpoint = os.environ.get("OPENSEARCH_ENDPOINT")
    user = os.environ.get("OPENSEARCH_USER", "admin")
    password = os.environ.get("OPENSEARCH_PASSWORD")
    if not endpoint:
        sys.exit("OPENSEARCH_ENDPOINT env var is required")
    if not password:
        sys.exit("OPENSEARCH_PASSWORD env var is required")
    return OpenSearch(
        hosts=[{"host": endpoint, "port": 9200}],
        http_auth=(user, password),
        use_ssl=True,
        verify_certs=False,
        connection_class=RequestsHttpConnection,
        timeout=60,
    )


INDEX = "elden-ring-entities"

from elden_ring._client import INDEX_MAPPING  # reuse mapping definition

# ---------------------------------------------------------------------------
# erdb source
# ---------------------------------------------------------------------------

# erdb only covers base-game patches (last updated 2023-07-31, frozen at 1.10.0).
# DLC content (patch 1.12+) is supplemented from the Discord bot CSVs instead.
ERDB_VERSIONS = [
    "1.10.0", "1.09.0", "1.08.1", "1.08.0", "1.07.1", "1.07.0",
    "1.06.0", "1.05.0", "1.04.2", "1.04.1",
    "1.03.3", "1.03.2", "1.03.1",
    "1.02.3", "1.02.2", "1.02.1",
]
ERDB_DEFAULT_VERSION = ERDB_VERSIONS[0]  # most recent base-game patch

# Patch version stamped on DLC supplement documents: these come from the Discord
# bot CSVs, last updated 2024-12-12 (after patch 1.16, before 1.17).
DLC_PATCH_VERSION = "1.16.0"

ERDB_ZIP_URL = (
    "https://github.com/EldenRingDatabase/erdb/raw/master"
    "/src/erdb/data/gamedata/{version}.zip"
)

# wepType param field → in-game weapon category display name.
# Values confirmed against known weapons extracted from EquipParamWeapon.csv.
WEAPON_TYPES: dict[int, str] = {
    1:  "Dagger",
    3:  "Straight Sword",
    5:  "Greatsword",
    7:  "Colossal Sword",
    9:  "Curved Sword",
    11: "Curved Greatsword",
    13: "Katana",
    14: "Twinblade",
    15: "Thrusting Sword",
    16: "Heavy Thrusting Sword",
    17: "Axe",
    19: "Greataxe",
    21: "Hammer",
    23: "Great Hammer",
    24: "Flail",
    25: "Spear",
    28: "Great Spear",
    29: "Halberd",
    31: "Scythe",
    35: "Fist",
    37: "Claw",
    39: "Whip",
    41: "Colossal Weapon",
    50: "Light Bow",
    51: "Bow",
    53: "Greatbow",
    55: "Crossbow",
    56: "Ballista",
    57: "Glintstone Staff",
    61: "Sacred Seal",
    65: "Small Shield",
    67: "Medium Shield",
    69: "Greatshield",
    87: "Torch",
}

# wepType values that are not equippable weapons (ammo, throwables, unarmed).
_SKIP_WEP_TYPES: frozenset[int] = frozenset({0, 33, 81, 83, 85, 86})

# Scaling grade thresholds (correctX param value → letter grade)
SCALING_TIERS = [(75, "S"), (60, "A"), (40, "B"), (25, "C"), (15, "D"), (1, "E")]

# protectorCategory param value → armor slot name.
ARMOR_CATEGORIES: dict[int, str] = {0: "Head", 1: "Body", 2: "Arms", 3: "Legs"}


# Legendary sorceries/incantations have no param encoding; identified by name.
_LEGENDARY_SPELLS: frozenset[str] = frozenset({
    "Comet Azur", "Founding Rain of Stars", "Stars of Ruin",
    "Ranni's Dark Moon", "Flame of the Fell God", "Elden Stars", "Greyoll's Roar",
})


def _variant_base_name(name: str) -> str | None:
    """Return the base talisman name for a +N or +N Variant name, or None."""
    m = re.match(r'^(.+?)\s+\+\d+(?:\s+Variant)?$', name)
    return m.group(1) if m else None


def _talisman_group(sort_id: int | None) -> str | None:
    """Derive talisman menu group label from sortId.

    Talismans fall into eight groups in the equipment screen, each occupying a
    1000-wide block starting at sortId 400000. Group 1 = 400xxx, Group 8 = 407xxx.
    Items outside that range (e.g. the Entwining Umbilical Cord at 999999) return None.
    """
    if sort_id is None:
        return None
    block = sort_id // 1000
    group = block - 399
    if 1 <= group <= 8:
        return f"Group {group}"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int(val) -> int | None:
    try:
        n = int(float(val))
        return n if n != 0 else None
    except (TypeError, ValueError):
        return None


def _float(val) -> float | None:
    try:
        n = round(float(val), 3)
        return n if n != 0.0 else None
    except (TypeError, ValueError):
        return None


def _scaling_grade(val) -> str | None:
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    for threshold, grade in SCALING_TIERS:
        if n >= threshold:
            return grade
    return None


def _load_fmg(z: zipfile.ZipFile, filename: str) -> dict[str, str]:
    """Parse an FMG XML file and return {id_str: text} for non-null entries."""
    if filename not in z.namelist():
        return {}
    with z.open(filename) as f:
        tree = ET.parse(f)
    return {
        e.get("id"): e.text
        for e in tree.iter("text")
        if e.text and "%null%" not in e.text
    }


def _csv_rows(z: zipfile.ZipFile, filename: str) -> list[dict]:
    """Parse a semicolon-delimited CSV from the zip and return all rows."""
    with z.open(filename) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter=";")
        return list(reader)


def _is_valid_row(row: dict) -> bool:
    name = row.get("Row Name", "")
    return bool(name) and not name.startswith("[") and name != "Row Name"


def _is_valid_spell_row(row: dict) -> bool:
    """Spell CSV rows are prefixed with [Sorcery] or [Incantation]; accept those, reject [ERROR] etc."""
    name = row.get("Row Name", "")
    return bool(name) and ("[Sorcery]" in name or "[Incantation]" in name)


# ---------------------------------------------------------------------------
# Japanese text enrichment — ihascats/Elden-Text-JP (base game only)
# ---------------------------------------------------------------------------

JP_BASE_GAME_URL = "https://raw.githubusercontent.com/ihascats/Elden-Text-JP/main/index.html"

# These sections use <h3>Name [id]</h3><p>description</p> format.
_JP_NAME_FORMAT_SECTIONS: frozenset[str] = frozenset({
    "WeaponName", "GemName", "GoodsName", "AccessoryName", "ProtectorName",
})

# NpcName FMG IDs for enemy entities — maps English names (as used in the Discord
# bot CSVs) to NpcName message IDs from ihascats/Elden-Text-JP. Base-game only;
# DLC/SOTE enemies are absent from that source and will have name_ja = null.
# IDs verified against the NpcName section of the JP FMG file.
_ENEMY_NPCNAME_IDS: dict[str, str] = {
    # --- Great Enemies (base game) ---
    "Margit, the Fell Omen":                        "902130000",
    "Morgott The Grace-Given Veiled Monarch Omen King": "902130002",
    "Godrick the Grafted":                          "904750000",
    "Godefroy The Grafted":                         "904750520",
    "Rennala Carian Queen of the Full Moon":        "120000",
    "Starscourge Radahn":                           "904730000",
    "Rykard, Lord of Blasphemy":                    "904710001",
    "God-Devouring Serpent":                        "904710000",
    "Mohg, Lord of Blood":                          "904800000",
    "Mohg, the Omen":                               "904800002",
    "Malenia, Blade of Miquella":                   "902120000",
    "Lichdragon Fortissax":                         "904510000",
    "Maliketh, The Black Blade":                    "902110001",
    "Astel, Naturalborn of the Void":               "904620001",
    "Astel, Stars of Darkness":                     "904620320",
    "Regal Ancestor Spirit":                        "904670001",
    "Ancestor Spirit":                              "904670000",
    "Radagon of the Golden Order":                  "902190000",
    "Godfrey, First Elden Lord":                    "904720000",
    "Godfrey, First Elden Lord (Golden Shade)":     "904720001",
    "Fire Giant":                                   "904760000",
    "Dragonlord Placidusax":                        "904520000",
    # --- Named bosses ---
    "Flying Dragon Agheel":                         "904500600",
    "Flying Dragon Greyll":                         "904500601",
    "Decaying Ekzykes":                             "904501600",
    "Glintstone Dragon Smarag":                     "904502600",
    "Glintstone Dragon Adula":                      "904502601",
    "Borealis, the Freezing Fog":                   "904503600",
    "Ancient Dragon Lansseax":                      "904510600",
    "MAGMA WYRM MAKAR":                             "904910000",
    "Magma Wyrm":                                   "904910320",
    "Great Wyrm Theodorix":                         "904911600",
    "Dragonkin Soldier":                            "904650600",
    "Dragonkin Soldier of Nokstella":               "904650000",
    "Red Wolf of Radagon":                          "903181000",
    "Red Wolf of the Champion":                     "903181300",
    "Valiant Gargoyle":                             "904770000",
    "Black Blade Kindred":                          "904770600",
    "Godskin Apostle":                              "903560000",
    "Godskin Noble":                                "903570000",
    "Godskin Duo":                                  "903575000",
    "Godskin Apostle and Godskin Noble":            "903575000",
    "Fell Twins":                                   "904820310",
    "Mimic Tear":                                   "903320300",
    "Crucible Knight Ordovis":                      "902500300",
    "Crucible Knights":                             "902500301",
    "Crucible Knight Siluria":                      "902500600",
    "Night's Cavalry":                              "903150600",
    "Black Knife Assassin":                         "902100300",
    "Alecto, Black Knife Ringleader":               "902100521",
    "Roundtable Knight Vyke":                       "900000521",
    "Commander O'Neil":                             "903050600",
    "Commander Niall":                              "903050500",
    "Elemer of the Briar":                          "903100500",
    "Bell Bearing Hunter":                          "903100600",
    "Loretta, Knight of the Haligtree":             "903252000",
    "Royal Knight Loretta":                         "903253500",
    "Tree Sentinel":                                "903251600",
    "Draconic Tree Sentinel":                       "903250600",
    "Bloodhound Knight":                            "904290310",
    "Bloodhound Knight Darriwil":                   "904290520",
    "Fallingstar Beast":                            "904680320",
    "Full-Grown Fallingstar Beast":                 "904680603",
    "Ulcerated Tree Spirit":                        "904640000",
    "Putrid Tree Spirit":                           "904640300",
    "Putrid Avatar":                                "904811600",
    "Erdtree Avatar":                               "904810600",
    "Tibia Mariner":                                "904950600",
    "Death Rite Bird":                              "904980600",
    "Deathbird":                                    "904980601",
    "Spiritcaller Snail":                           "904140300",
    "Runebear":                                     "904630310",
    "Stonedigger Troll":                            "904600320",
    "Bols, Carian Knight":                          "904600520",
    "Grafted Scion":                                "904690000",
    "Mad Pumpkin Head":                             "904340540",
    "Erdtree Burial Watchdog":                      "904260300",
    "Royal Revenant":                               "904020540",
    "Beastman of Farum Azula":                      "903970310",
    "Wormface":                                     "904580600",
    "Abductor Virgins":                             "904470000",
    "Miranda The Blighted Bloom":                   "904480310",
    "Demi-Human Chiefs":                            "904120310",
    "Demi-Human Queen Margot":                      "904130310",
    "Demi-Human Queen Gilika":                      "904130540",
    "Demi-Human Queen Maggie":                      "904130600",
    "Onyx Lord":                                    "903600320",
    "Ancient Hero of Zamor":                        "907100300",
    "Grave Warden Duelist":                         "903400300",
    "Putrid Grave Warden Duelist":                  "903400302",
    "Cleanrot Knight":                              "903800310",
    "Scaly Misbegotten":                            "903451320",
    "Misbegotten Warrior":                          "903460300",
    "Misbegotten Crusader":                         "903460310",
    "Leonine Misbegotten":                          "903460500",
    "Battlemage Hugues":                            "903704520",
    "Guardian Golem":                               "904660310",
    "Perfumer Tricia and Misbegotten Warrior":      "903700300",
    # --- NPC-bosses (6-digit NpcName IDs) ---
    "Adan, Thief of Fire":                          "135600",
    "Esgar, Priest of Blood":                       "138600",
    "Patches":                                      "130900",
    "Necromancer Garris":                           "137600",
    "Sanguine Noble":                               "134310",
}


def _parse_jp_name_section(sec_html: str) -> dict[str, dict[str, str]]:
    """Parse h3+p HTML → {id: {"name": str, "description": str}}."""
    result: dict[str, dict[str, str]] = {}
    current_id: str | None = None
    current_name: str | None = None
    desc_parts: list[str] = []

    for m in re.finditer(r'<h3>([^<]+)</h3>|<p>(.*?)</p>', sec_html, re.DOTALL):
        if m.group(1) is not None:
            if current_id is not None:
                result[current_id] = {
                    "name": current_name,
                    "description": " ".join(" ".join(desc_parts).split()),
                }
            text = m.group(1).strip()
            id_m = re.match(r'^(.+?)\s+\[(\d+)\]$', text)
            if id_m:
                current_name = id_m.group(1).strip()
                current_id = id_m.group(2)
                desc_parts = []
            else:
                current_id = None
        elif current_id is not None and m.group(2) is not None:
            p_text = " ".join(m.group(2).split())
            if p_text:
                desc_parts.append(p_text)

    if current_id is not None:
        result[current_id] = {
            "name": current_name,
            "description": " ".join(" ".join(desc_parts).split()),
        }
    return result


def _parse_jp_info_section(sec_html: str) -> dict[str, str]:
    """Parse [id] text HTML → {id: text}. Strips h3 entries first to avoid ID collisions."""
    clean = re.sub(r'<h3>[^<]*</h3>', '', sec_html)
    result: dict[str, str] = {}
    for m in re.finditer(r'\[(\d+)\]\s*([^<\[]+)', clean):
        id_ = m.group(1)
        text = " ".join(m.group(2).split()).strip()
        if text:
            result[id_] = text
    return result


def _load_jp_fmgs() -> dict[str, dict] | None:
    """Download and parse ihascats/Elden-Text-JP into section dicts.

    Name-format sections return {id: {"name": str, "description": str}}.
    Info-format sections (TalkMsg, NpcName, etc.) return {id: str}.
    Returns None on download failure; callers silently skip JP enrichment.
    """
    print("  Downloading Japanese FMG data …")
    try:
        resp = requests.get(JP_BASE_GAME_URL, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Warning: failed to download JP FMGs: {e}")
        return None

    html = resp.text
    parts = re.split(r'<h2>([^<]+)</h2>', html)
    parsed: dict[str, dict] = {}
    for i in range(1, len(parts), 2):
        sec_name = parts[i].replace(".fmg", "").strip()
        sec_html = parts[i + 1] if i + 1 < len(parts) else ""
        if sec_name in _JP_NAME_FORMAT_SECTIONS:
            parsed[sec_name] = _parse_jp_name_section(sec_html)
        else:
            parsed[sec_name] = _parse_jp_info_section(sec_html)

    counts = {k: len(v) for k, v in parsed.items() if v}
    print(f"  JP FMGs loaded: {counts}")
    return parsed


def _jp_name_desc(jp_fmgs: dict | None, section: str, row_id: str) -> tuple[str | None, str | None]:
    """Return (name_ja, description_ja) from a JP name-format section, or (None, None)."""
    if jp_fmgs is None:
        return None, None
    entry = jp_fmgs.get(section, {}).get(row_id)
    if not isinstance(entry, dict):
        return None, None
    return entry.get("name") or None, entry.get("description") or None


def _count_ja(docs: list[dict]) -> int:
    return sum(1 for d in docs if d.get("name_ja"))


# ---------------------------------------------------------------------------
# Weapons
# ---------------------------------------------------------------------------

def _parse_weapons(z: zipfile.ZipFile, patch_version: str, location_map: dict[str, list[str]] | None = None, jp_fmgs: dict | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    names = _load_fmg(z, "WeaponName.fmg.xml")
    captions = _load_fmg(z, "WeaponCaption.fmg.xml")
    rows = _csv_rows(z, "EquipParamWeapon.csv")

    docs = []
    for row in rows:
        if not _is_valid_row(row):
            continue

        wep_type_id = int(float(row.get("wepType", 0) or 0))
        if wep_type_id in _SKIP_WEP_TYPES:
            continue  # ammo, throwables, unarmed

        row_id = row["Row ID"]
        name = names.get(row_id) or row.get("Row Name", "")
        if not name or name.startswith("["):
            continue

        # Discard placeholder rows that carry no meaningful attack data and
        # have no in-game description (internal engine entries).
        has_attack = any(
            float(row.get(f, 0) or 0) > 0
            for f in ("attackBasePhysics", "attackBaseMagic", "attackBaseFire",
                      "attackBaseThunder", "attackBaseDark")
        )
        description = captions.get(row_id, "")
        if not has_attack and not description:
            continue

        cat_name = WEAPON_TYPES.get(wep_type_id, f"weapon_type_{wep_type_id}")
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None
        name_ja, description_ja = _jp_name_desc(jp_fmgs, "WeaponName", row_id)
        sort_id = _int(row.get("sortId"))
        rarity = int(float(row.get("rarity", 0) or 0))
        trophy_grade = int(float(row.get("trophySGradeId", -1) or -1))
        is_legendary = rarity == 3 and trophy_grade >= 0

        docs.append({
            "entity_type": "weapon",
            "name": name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": "\n\n".join(filter(None, [
                description,
                f"Weapon type: {cat_name}",
                f"Found in: {loc_str}" if loc_str else None,
            ])),
            "tags": [cat_name],
            "location": loc_str,
            "sort_id":          sort_id,
            "menu_category":    cat_name,
            "is_legendary":     True if is_legendary else None,
            "achievement_set":  "Legendary Armaments" if is_legendary else None,
            **_acquisition_fields(name, drop_map, merchant_items),
            "weight":           _float(row.get("weight")),
            "attack_physical":  _int(row.get("attackBasePhysics")),
            "attack_magic":     _int(row.get("attackBaseMagic")),
            "attack_fire":      _int(row.get("attackBaseFire")),
            "attack_lightning": _int(row.get("attackBaseThunder")),
            "attack_holy":      _int(row.get("attackBaseDark")),  # "dark" = holy in ER
            "scaling_str":      _scaling_grade(row.get("correctStrength")),
            "scaling_dex":      _scaling_grade(row.get("correctAgility")),
            "scaling_int":      _scaling_grade(row.get("correctMagic")),
            "scaling_fai":      _scaling_grade(row.get("correctFaith")),
            "scaling_arc":      _scaling_grade(row.get("correctLuck")),
            "req_str":          _int(row.get("properStrength")),
            "req_dex":          _int(row.get("properAgility")),
            "req_int":          _int(row.get("properMagic")),
            "req_fai":          _int(row.get("properFaith")),
            "req_arc":          _int(row.get("properLuck")),
            "name_ja":          name_ja,
            "description_ja":   description_ja,
        })

    return docs


# ---------------------------------------------------------------------------
# Armor
# ---------------------------------------------------------------------------

def _parse_armor(z: zipfile.ZipFile, patch_version: str, location_map: dict[str, list[str]] | None = None, jp_fmgs: dict | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    names = _load_fmg(z, "ProtectorName.fmg.xml")
    captions = _load_fmg(z, "ProtectorCaption.fmg.xml")
    rows = _csv_rows(z, "EquipParamProtector.csv")

    docs = []
    for row in rows:
        if not _is_valid_row(row):
            continue

        row_id = row["Row ID"]
        name = names.get(row_id) or row.get("Row Name", "")
        if not name or name.startswith("["):
            continue

        weight = _float(row.get("weight"))
        if weight is None:
            continue  # skip weightless placeholder rows

        description = captions.get(row_id, "")
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None
        name_ja, description_ja = _jp_name_desc(jp_fmgs, "ProtectorName", row_id)
        sort_id = _int(row.get("sortId"))
        armor_cat_id = int(float(row.get("protectorCategory", 0) or 0))
        armor_cat = ARMOR_CATEGORIES.get(armor_cat_id)

        docs.append({
            "entity_type": "armor",
            "name": name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": "\n\n".join(filter(None, [description, f"Found in: {loc_str}" if loc_str else None])),
            "tags": [armor_cat] if armor_cat else [],
            "location": loc_str,
            "sort_id":          sort_id,
            "menu_category":    armor_cat,
            **_acquisition_fields(name, drop_map, merchant_items),
            "weight": weight,
            # Defense cut rates (0-1 scale → stored as-is for filtering)
            # physical defense is split across several sub-types; store the main cut rate
            "defense_physical":  _float(row.get("defensePhysics")),
            "defense_magic":     _float(row.get("defenseMagic")),
            "defense_fire":      _float(row.get("defenseFire")),
            "defense_lightning": _float(row.get("defenseThunder")),
            "defense_holy":      _float(row.get("defenseDark")),
            "name_ja":           name_ja,
            "description_ja":    description_ja,
        })

    return docs


# ---------------------------------------------------------------------------
# Spells (sorceries and incantations)
# ---------------------------------------------------------------------------

def _parse_spells(z: zipfile.ZipFile, patch_version: str, location_map: dict[str, list[str]] | None = None, jp_fmgs: dict | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    # Spell names/descriptions live in the Goods FMG (spells are "goods" in the param system)
    names = _load_fmg(z, "GoodsName.fmg.xml")
    captions = _load_fmg(z, "GoodsCaption.fmg.xml")
    rows = _csv_rows(z, "Magic.csv")

    docs = []
    for row in rows:
        if not _is_valid_spell_row(row):
            continue

        row_id = row["Row ID"]
        internal_name = row.get("Row Name", "")

        # Internal names are prefixed: "[Sorcery] Glintstone Pebble"
        spell_type = "Sorcery" if "[Sorcery]" in internal_name else "Incantation"
        name = names.get(row_id) or internal_name.split("] ", 1)[-1]
        if not name or name.startswith("["):
            continue

        description = captions.get(row_id, "")
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None
        name_ja, description_ja = _jp_name_desc(jp_fmgs, "GoodsName", row_id)
        is_legendary = name in _LEGENDARY_SPELLS

        docs.append({
            "entity_type": "spell",
            "name": name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": "\n\n".join(filter(None, [description, f"Found in: {loc_str}" if loc_str else None])),
            "tags": [spell_type],
            "location": loc_str,
            "sort_id":        _int(row.get("sortId")),
            "menu_category":  spell_type,
            "is_legendary":   True if is_legendary else None,
            "achievement_set": "Legendary Sorceries and Incantations" if is_legendary else None,
            "fp_cost":        _int(row.get("mp")),
            "slots":          _int(row.get("slotLength")),
            "req_int":        _int(row.get("requirementIntellect")),
            "req_fai":        _int(row.get("requirementFaith")),
            "name_ja":        name_ja,
            "description_ja": description_ja,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    return docs


# ---------------------------------------------------------------------------
# Ashes of War
# ---------------------------------------------------------------------------

def _parse_ashes_of_war(z: zipfile.ZipFile, patch_version: str, location_map: dict[str, list[str]] | None = None, jp_fmgs: dict | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    names = _load_fmg(z, "GemName.fmg.xml")
    captions = _load_fmg(z, "GemCaption.fmg.xml")
    infos = _load_fmg(z, "GemInfo.fmg.xml")
    rows = _csv_rows(z, "EquipParamGem.csv")

    docs = []
    for row in rows:
        if not _is_valid_row(row):
            continue

        row_id = row["Row ID"]
        name = names.get(row_id) or row.get("Row Name", "")
        if not name or name.startswith("["):
            continue

        caption = captions.get(row_id, "")
        info = infos.get(row_id, "")
        description = caption or info
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None
        text_content = "\n\n".join(filter(None, [caption, info, f"Found in: {loc_str}" if loc_str else None]))
        name_ja, description_ja = _jp_name_desc(jp_fmgs, "GemName", row_id)

        docs.append({
            "entity_type": "ash_of_war",
            "name": name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": text_content or description,
            "tags": [],
            "location": loc_str,
            "sort_id":        _int(row.get("sortId")),
            "name_ja":        name_ja,
            "description_ja": description_ja,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    return docs


# ---------------------------------------------------------------------------
# Talismans
# ---------------------------------------------------------------------------

def _load_discord_bot_talismans() -> dict[str, dict]:
    """Fetch Discord bot talismans.csv and return normalized-name → row dict.

    Keys strip the ' Variant' suffix so they match erdb names. Each row also
    gets a '_normalized_name' key with the stripped name.
    """
    print("  Downloading talismans.csv (Discord bot) …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/talismans.csv", timeout=30)
    resp.raise_for_status()
    result: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if name:
            normalized = re.sub(r"\s+Variant$", "", name)
            result[normalized] = {**row, "_normalized_name": normalized}
    return result


def _extract_effect_value(effect: str) -> float | None:
    """Extract the first numeric value from a talisman effect string."""
    m = re.search(r"(\d+(?:\.\d+)?)", effect)
    return float(m.group(1)) if m else None


def _parse_talismans(z: zipfile.ZipFile, patch_version: str, location_map: dict[str, list[str]] | None = None, jp_fmgs: dict | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None, discord_bot_map: dict[str, dict] | None = None) -> list[dict]:
    names = _load_fmg(z, "AccessoryName.fmg.xml")
    captions = _load_fmg(z, "AccessoryCaption.fmg.xml")
    rows = _csv_rows(z, "EquipParamAccessory.csv")

    docs = []
    for row in rows:
        if not _is_valid_row(row):
            continue

        row_id = row["Row ID"]
        name = names.get(row_id) or row.get("Row Name", "")
        if not name or name.startswith("["):
            continue

        description = captions.get(row_id, "")
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None
        name_ja, description_ja = _jp_name_desc(jp_fmgs, "AccessoryName", row_id)
        sort_id = _int(row.get("sortId"))
        comp_trophy_sed = int(float(row.get("compTrophySedId", 0) or 0))
        is_legendary = comp_trophy_sed == 17
        weight = _float(row.get("weight"))

        # Effect text comes from Discord bot (not erdb). Only stamp it on the
        # latest base-game patch to avoid version anachronism: the Discord bot
        # is a single snapshot with no per-patch history.
        bot_row = (discord_bot_map or {}).get(name) if patch_version == ERDB_DEFAULT_VERSION else None
        effect = bot_row.get("effect", "").strip() if bot_row else None
        effect_value = _extract_effect_value(effect) if effect else None

        docs.append({
            "entity_type": "item",
            "name": name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": "\n\n".join(filter(None, [description, f"Found in: {loc_str}" if loc_str else None])),
            "tags": ["Talisman"],
            "location": loc_str,
            "weight":         weight,
            "effect":         effect,
            "effect_value":   effect_value,
            "sort_id":        sort_id,
            "menu_category":  _talisman_group(sort_id),
            "base_item":      _variant_base_name(name),
            "is_legendary":   True if is_legendary else None,
            "achievement_set": "Legendary Talismans" if is_legendary else None,
            "name_ja":        name_ja,
            "description_ja": description_ja,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    return docs


# ---------------------------------------------------------------------------
# NPC dialogue — fromsoft-fts
# ---------------------------------------------------------------------------

FROMSOFT_FTS_URL = (
    "https://raw.githubusercontent.com/tefkah/fromsoft-fts/main/assets/data.json"
)


def load_fromsoft_fts(jp_fmgs: dict | None = None) -> list[dict]:
    """Download and parse NPC dialogue from the fromsoft-fts community dataset.

    Source: https://github.com/tefkah/fromsoft-fts
    Coverage: ~217 speakers (50 named, 167 unattributed). Named entries include
    major NPCs (Melina, Gideon, Ranni, etc.). Unattributed entries are narration,
    ambient lines, or minor characters with no datamined name.

    One document per speaker — all conversation sections concatenated into
    text_content so full-text search works across an NPC's entire dialogue.
    """
    print("  Downloading NPC dialogue from fromsoft-fts …")
    resp = requests.get(FROMSOFT_FTS_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    talk_msg_jp: dict[str, str] = (jp_fmgs or {}).get("TalkMsg", {})

    docs = []
    for entry in data.get("dialogue", []):
        npc_id = entry.get("npcId", "")
        name = entry.get("name", "").strip()
        if not name:
            name = f"Unknown NPC (id:{npc_id})"

        # Flatten all sections → lines into a single block of text.
        # Each section is a distinct conversation state; separating them with
        # blank lines preserves the structure for human readers while keeping
        # the full text searchable as one document.
        section_texts = []
        lines_ja: list[str] = []
        for section in entry.get("sections", []):
            lines = [l["text"] for l in section.get("lines", []) if l.get("text", "").strip()]
            if lines:
                section_texts.append("\n".join(lines))
            if talk_msg_jp:
                for l in section.get("lines", []):
                    jp_text = talk_msg_jp.get(str(l.get("id", "")), "")
                    if jp_text:
                        lines_ja.append(jp_text)

        text_content = "\n\n".join(section_texts)
        if not text_content.strip():
            continue

        docs.append({
            "entity_type": "npc_dialogue",
            "name": name,
            "patch_version": DLC_PATCH_VERSION,  # dataset does not version
            "source": "fromsoft-fts",
            "description": text_content[:500],  # first ~500 chars as summary
            "text_content": text_content,
            "tags": ["named_npc"] if entry.get("name", "").strip() else ["unattributed"],
            "npc_id": npc_id,
            "text_content_ja": "\n".join(lines_ja) if lines_ja else None,
        })

    named = sum(1 for d in docs if "named_npc" in d["tags"])
    ja_count = sum(1 for d in docs if d.get("text_content_ja"))
    print(f"  Parsed: {{'npc_dialogue': {len(docs)}}} ({named} named, {len(docs) - named} unattributed)")
    if talk_msg_jp:
        print(f"  JP dialogue coverage: {ja_count}/{len(docs)} NPC documents have Japanese text")
    return docs


# ---------------------------------------------------------------------------
# Discord bot scraped CSVs — enemies and item locations
# ---------------------------------------------------------------------------

DISCORD_BOT_BASE = (
    "https://raw.githubusercontent.com/xS2RT/EldenRingDiscordBot/main/eldenringScrap"
)


def _parse_python_literal(s: str):
    """Parse a Python list/dict literal string from the Discord bot CSVs."""
    if not s or s.strip() in ("", "nan", "None", "[]", "{}"):
        return None
    try:
        return ast.literal_eval(s.strip())
    except (ValueError, SyntaxError):
        return None


def _build_drop_map() -> dict[str, dict[str, list[str]]]:
    """Build item_name → {"boss_drop": [boss_names], "enemy_drop": [creature_names]}.

    Downloads bosses.csv and creatures.csv from the Discord bot. Used to annotate
    item documents with acquisition_types / acquisition_sources before indexing.
    """
    print("  Building drop map (bosses.csv + creatures.csv) …")
    drop_map: dict[str, dict[str, list[str]]] = {}

    resp = requests.get(f"{DISCORD_BOT_BASE}/bosses.csv", timeout=30)
    resp.raise_for_status()
    for row in csv.DictReader(io.StringIO(resp.text)):
        boss_name = row.get("name", "").strip()
        if not boss_name:
            continue
        locs_drops = _parse_python_literal(row.get("Locations & Drops", ""))
        if not isinstance(locs_drops, dict):
            continue
        for items_list in locs_drops.values():
            for item in items_list:
                item = item.strip() if isinstance(item, str) else str(item)
                # Skip rune amounts (digits + commas) and empty strings
                if not item or item.replace(",", "").replace(" ", "").isdigit():
                    continue
                entry = drop_map.setdefault(item, {})
                entry.setdefault("boss_drop", [])
                if boss_name not in entry["boss_drop"]:
                    entry["boss_drop"].append(boss_name)

    resp = requests.get(f"{DISCORD_BOT_BASE}/creatures.csv", timeout=30)
    resp.raise_for_status()
    for row in csv.DictReader(io.StringIO(resp.text)):
        creature_name = row.get("name", "").strip()
        if not creature_name:
            continue
        drops = _parse_python_literal(row.get("drops", "")) or []
        for item in drops:
            item = item.strip() if isinstance(item, str) else str(item)
            if not item or item == "???":
                continue
            entry = drop_map.setdefault(item, {})
            entry.setdefault("enemy_drop", [])
            if creature_name not in entry["enemy_drop"]:
                entry["enemy_drop"].append(creature_name)

    total = sum(1 for v in drop_map.values() if v)
    print(f"  Drop map: {total} items with known drop sources")
    return drop_map


def _extract_merchant_items(z: zipfile.ZipFile) -> dict[str, list[str]]:
    """Return item_name → [vendor_names] from ShopLineupParam, applying known overrides."""
    rows = _csv_rows(z, "ShopLineupParam.csv")
    item_to_vendors: dict[str, list[str]] = {}

    for row in rows:
        row_name = row.get("Row Name", "").strip()
        m = re.match(r"^\[(.+?)\]\s*(.+)$", row_name)
        if not m:
            continue
        full_vendor = m.group(1).strip()
        item_name = m.group(2).strip()
        base_vendor, _, _ = full_vendor.partition(" - ")
        base_vendor = base_vendor.strip()
        if base_vendor in _SKIP_SHOP_NAMES:
            continue
        item_to_vendors.setdefault(item_name, [])
        if base_vendor not in item_to_vendors[item_name]:
            item_to_vendors[item_name].append(base_vendor)

    # Apply same vendor corrections as _parse_merchants
    for (wrong_vendor, item_name), correct_vendor in _VENDOR_ITEM_OVERRIDES.items():
        if item_name in item_to_vendors and wrong_vendor in item_to_vendors[item_name]:
            item_to_vendors[item_name].remove(wrong_vendor)
            if correct_vendor not in item_to_vendors[item_name]:
                item_to_vendors[item_name].append(correct_vendor)

    return item_to_vendors


def _acquisition_fields(
    name: str,
    drop_map: dict[str, dict[str, list[str]]] | None,
    merchant_items: dict[str, list[str]] | None,
) -> dict:
    """Return acquisition edge fields (omitted if empty).

    Emits:
      acquisition_types   — ["merchant", "boss_drop", "enemy_drop"] (whichever apply)
      acquisition_sources — flat list of all source names (merged)
      dropped_by          — enemies/bosses that drop this item (boss_drop + enemy_drop)
      sold_by             — merchants that sell this item
    """
    types: list[str] = []
    sources: list[str] = []
    dropped_by: list[str] = []
    sold_by: list[str] = []

    if merchant_items and name in merchant_items:
        types.append("merchant")
        sold_by = merchant_items[name]
        sources.extend(sold_by)

    if drop_map and name in drop_map:
        entry = drop_map[name]
        if "boss_drop" in entry:
            types.append("boss_drop")
            dropped_by.extend(entry["boss_drop"])
            sources.extend(entry["boss_drop"])
        if "enemy_drop" in entry:
            types.append("enemy_drop")
            dropped_by.extend(entry["enemy_drop"])
            sources.extend(entry["enemy_drop"])

    result = {}
    if types:
        result["acquisition_types"] = types
    if sources:
        result["acquisition_sources"] = sources
    if dropped_by:
        result["dropped_by"] = dropped_by
    if sold_by:
        result["sold_by"] = sold_by
    return result


def _enemy_name_ja(name: str, jp_fmgs: dict | None) -> str | None:
    """Return the Japanese name for an enemy, or None if unavailable."""
    if jp_fmgs is None:
        return None
    npc_name_id = _ENEMY_NPCNAME_IDS.get(name)
    if npc_name_id is None:
        return None
    return jp_fmgs.get("NpcName", {}).get(npc_name_id) or None


def load_discord_bot_enemies(jp_fmgs: dict | None = None) -> list[dict]:
    """Parse enemy data from the Discord bot bosses.csv and creatures.csv.

    bosses.csv  — major bosses with per-location rune/drop data
    creatures.csv — regular enemies with location + drop lists and lore blurbs
    """
    docs: list[dict] = []

    # --- Bosses ---
    print("  Downloading bosses.csv …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/bosses.csv", timeout=30)
    resp.raise_for_status()
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if not name:
            continue

        hp_raw = row.get("HP", "").strip()
        lore = row.get("blockquote", "").strip()

        locs_drops = _parse_python_literal(row.get("Locations & Drops", ""))
        locations: list[str] = []
        drops: list[str] = []
        if isinstance(locs_drops, dict):
            for loc_key, items_list in locs_drops.items():
                if not isinstance(loc_key, str):
                    continue
                locations.append(loc_key.rstrip(":").strip())
                for item in items_list:
                    item = item.strip()
                    # Skip pure rune amounts (digits + commas only)
                    if item and not item.replace(",", "").replace(" ", "").isdigit():
                        drops.append(item)

        parts: list[str] = []
        if hp_raw:
            parts.append(f"HP: {hp_raw}")
        if locations:
            parts.append("Locations: " + ", ".join(locations))
        if drops:
            parts.append("Drops: " + ", ".join(drops))
        if lore:
            parts.append(lore)
        text_content = "\n".join(parts)

        name_ja = _enemy_name_ja(name, jp_fmgs)
        docs.append({
            "entity_type": "enemy",
            "name": name,
            "name_ja": name_ja,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": lore[:500] if lore else text_content[:500],
            "text_content": text_content,
            "tags": ["boss"],
            "location": ", ".join(locations) if locations else None,
        })

    boss_count = len(docs)

    # --- Creatures ---
    print("  Downloading creatures.csv …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/creatures.csv", timeout=30)
    resp.raise_for_status()
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if not name:
            continue

        lore = row.get("blockquote", "").strip()
        locations = _parse_python_literal(row.get("locations", "")) or []
        drops = _parse_python_literal(row.get("drops", "")) or []

        parts = []
        if locations:
            parts.append("Locations: " + ", ".join(str(l) for l in locations))
        if drops:
            parts.append("Drops: " + ", ".join(str(d) for d in drops))
        if lore:
            parts.append(lore)
        text_content = "\n".join(parts)

        name_ja = _enemy_name_ja(name, jp_fmgs)
        docs.append({
            "entity_type": "enemy",
            "name": name,
            "name_ja": name_ja,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": lore[:500] if lore else text_content[:500],
            "text_content": text_content,
            "tags": ["creature"],
            "location": ", ".join(str(l) for l in locations) if locations else None,
        })

    creature_count = len(docs) - boss_count
    ja_count = sum(1 for d in docs if d.get("name_ja"))
    print(f"  Parsed: {{'enemy': {len(docs)}}} ({boss_count} bosses, {creature_count} creatures, {ja_count} with name_ja)")
    return docs


def load_location_map() -> dict[str, list[str]]:
    """Download locations.csv and build a reverse map: item_name → [location_names].

    The Discord bot's locations.csv lists every in-game location and the items
    found there. Inverting it gives a lookup table that the erdb parsers can use
    to attach a `location` field to weapon/armor/spell/talisman documents.
    """
    print("  Downloading locations.csv …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/locations.csv", timeout=30)
    resp.raise_for_status()

    item_to_locations: dict[str, list[str]] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        loc_name = row.get("name", "").strip()
        if not loc_name:
            continue
        items = _parse_python_literal(row.get("items", "")) or []
        for item in items:
            item_str = item.strip() if isinstance(item, str) else str(item)
            if item_str:
                item_to_locations.setdefault(item_str, []).append(loc_name)

    print(f"  Location map: {len(item_to_locations)} unique items with known locations")
    return item_to_locations


def _supplement_aow(erdb_docs: list[dict], location_map: dict[str, list[str]] | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    """Return Discord bot AoW docs for the 26 DLC entries missing from erdb FMGs.

    erdb 1.10.0's GemName.fmg.xml returns '[ERROR]' for all Shadow of the Erdtree
    ashes of war, so _parse_ashes_of_war() skips them. This function downloads the
    Discord bot's ashesOfWar.csv and skills.csv (for effect text), filters to only
    the entries not already present in erdb_docs, and returns supplement documents.
    """
    known_names = {d["name"] for d in erdb_docs}

    print("  Downloading ashesOfWar.csv + skills.csv (DLC supplement) …")
    resp_aow = requests.get(f"{DISCORD_BOT_BASE}/ashesOfWar.csv", timeout=30)
    resp_aow.raise_for_status()
    resp_skills = requests.get(f"{DISCORD_BOT_BASE}/skills.csv", timeout=30)
    resp_skills.raise_for_status()

    # Build skill name → effect text lookup
    skill_effects: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(resp_skills.text)):
        skill_name = row.get("name", "").strip()
        effect = row.get("effect", "").strip()
        if skill_name and effect:
            skill_effects[skill_name] = effect

    docs: list[dict] = []
    for row in csv.DictReader(io.StringIO(resp_aow.text)):
        name = row.get("name", "").strip()
        if not name or name in known_names:
            continue
        skill_name = row.get("skill", "").strip()
        affinity = row.get("affinity", "").strip()
        intro = row.get("description", "").strip()
        effect = skill_effects.get(skill_name, "")
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None

        parts = []
        if affinity and affinity.lower() != "none":
            parts.append(f"Affinity: {affinity}")
        if intro:
            parts.append(intro)
        if effect:
            parts.append(f"{skill_name}: {effect}")
        if loc_str:
            parts.append(f"Found in: {loc_str}")
        text_content = "\n".join(parts)

        docs.append({
            "entity_type": "ash_of_war",
            "name": name,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": text_content[:500],
            "text_content": text_content,
            "tags": [affinity] if affinity else [],
            "location": loc_str,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    print(f"  AoW supplement: {len(docs)} DLC entries added")
    return docs


def _supplement_weapons(erdb_docs: list[dict], location_map: dict[str, list[str]] | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    """Return Discord bot weapon docs for DLC entries missing from erdb.

    erdb 1.10.0 predates the Shadow of the Erdtree DLC (patch 1.12+), so new weapon
    categories (Hand-to-Hand Arts, Thrusting Shields, Great Katanas, etc.) and all
    DLC-only weapons are absent. This supplement adds any weapon not already present
    in erdb_docs by name. Attack values and scaling grades are unavailable from this
    source; only descriptive and stat-requirement fields are populated.
    """
    known_names = {d["name"] for d in erdb_docs}

    print("  Downloading weapons.csv (DLC supplement) …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/weapons.csv", timeout=30)
    resp.raise_for_status()

    docs: list[dict] = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if not name or name in known_names:
            continue

        description = row.get("description", "").strip()
        category = row.get("category", "").strip()
        weight = _float(row.get("weight"))
        skill = row.get("skill", "").strip()

        reqs = _parse_python_literal(row.get("requirements", ""))
        req_str = _int(reqs.get("Str")) if isinstance(reqs, dict) else None
        req_dex = _int(reqs.get("Dex")) if isinstance(reqs, dict) else None
        req_int = _int(reqs.get("Int")) if isinstance(reqs, dict) else None
        req_fai = _int(reqs.get("Fai")) if isinstance(reqs, dict) else None
        req_arc = _int(reqs.get("Arc")) if isinstance(reqs, dict) else None
        locs = (location_map or {}).get(name)
        loc_str = ", ".join(locs) if locs else None

        parts = [description] if description else []
        if category:
            parts.append(f"Weapon type: {category}")
        if skill:
            parts.append(f"Skill: {skill}")
        if loc_str:
            parts.append(f"Found in: {loc_str}")

        docs.append({
            "entity_type": "weapon",
            "name": name,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": description,
            "text_content": "\n".join(parts),
            "tags": [category] if category else [],
            "location": loc_str,
            "weight": weight,
            "req_str": req_str,
            "req_dex": req_dex,
            "req_int": req_int,
            "req_fai": req_fai,
            "req_arc": req_arc,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    print(f"  Weapon supplement: {len(docs)} DLC entries added")
    return docs


def _supplement_armor(erdb_docs: list[dict], drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    """Return Discord bot armor docs for DLC entries missing from erdb."""
    known_names = {d["name"] for d in erdb_docs}

    print("  Downloading armors.csv (DLC supplement) …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/armors.csv", timeout=30)
    resp.raise_for_status()

    docs: list[dict] = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if not name or name in known_names:
            continue

        description = row.get("description", "").strip()
        armor_type = row.get("type", "").strip()
        weight = _float(row.get("weight"))
        location = row.get("how to acquire", "").strip() or None

        neg_raw = _parse_python_literal(row.get("damage negation", ""))
        neg = neg_raw[0] if isinstance(neg_raw, list) and neg_raw else {}
        def_physical  = _float(neg.get("Phy"))
        def_magic     = _float(neg.get("Mag"))
        def_fire      = _float(neg.get("Fir"))
        def_lightning = _float(neg.get("Lit"))
        def_holy      = _float(neg.get("Hol"))

        parts = [description] if description else []
        if location:
            parts.append(f"Found in: {location}")

        docs.append({
            "entity_type": "armor",
            "name": name,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": description,
            "text_content": "\n".join(parts),
            "tags": [armor_type] if armor_type else [],
            "location": location,
            "weight": weight,
            "defense_physical":  def_physical,
            "defense_magic":     def_magic,
            "defense_fire":      def_fire,
            "defense_lightning": def_lightning,
            "defense_holy":      def_holy,
            **_acquisition_fields(name, drop_map, merchant_items),
        })

    print(f"  Armor supplement: {len(docs)} DLC entries added")
    return docs


def _supplement_spells(erdb_docs: list[dict], drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None) -> list[dict]:
    """Return Discord bot spell docs for DLC sorceries and incantations missing from erdb."""
    known_names = {d["name"] for d in erdb_docs}

    docs: list[dict] = []
    for csv_name, spell_type in (("sorceries.csv", "Sorcery"), ("incantations.csv", "Incantation")):
        print(f"  Downloading {csv_name} (DLC supplement) …")
        resp = requests.get(f"{DISCORD_BOT_BASE}/{csv_name}", timeout=30)
        resp.raise_for_status()

        for row in csv.DictReader(io.StringIO(resp.text)):
            name = row.get("name", "").strip()
            if not name or name in known_names:
                continue

            description = row.get("description", "").strip()
            effect = row.get("effect", "").strip()
            location = row.get("location", "").strip() or None

            parts = [description] if description else []
            if effect and effect != description:
                parts.append(effect)
            if location:
                parts.append(f"Found in: {location}")

            docs.append({
                "entity_type": "spell",
                "name": name,
                "patch_version": DLC_PATCH_VERSION,
                "source": "fextralife-discord-bot",
                "description": description,
                "text_content": "\n".join(parts),
                "tags": [spell_type],
                "location": location,
                "fp_cost": _int(row.get("FP")),
                "slots":   _int(row.get("slot")),
                "req_int": _int(row.get("INT")),
                "req_fai": _int(row.get("FAI")),
                "req_arc": _int(row.get("ARC")),
                **_acquisition_fields(name, drop_map, merchant_items),
            })

    print(f"  Spell supplement: {len(docs)} DLC entries added")
    return docs


def _supplement_talismans(erdb_docs: list[dict], location_map: dict[str, list[str]] | None = None, drop_map: dict[str, dict[str, list[str]]] | None = None, merchant_items: dict[str, list[str]] | None = None, discord_bot_map: dict[str, dict] | None = None) -> list[dict]:
    """Return Discord bot talisman docs for DLC entries missing from erdb."""
    known_names = {d["name"] for d in erdb_docs}

    if discord_bot_map is None:
        discord_bot_map = _load_discord_bot_talismans()

    docs: list[dict] = []
    for normalized_name, row in discord_bot_map.items():
        if not normalized_name or normalized_name in known_names:
            continue

        description = row.get("description", "").strip()
        effect = row.get("effect", "").strip() or None
        effect_value = _extract_effect_value(effect) if effect else None
        weight = _float(row.get("weight"))
        locs = (location_map or {}).get(normalized_name)
        loc_str = ", ".join(locs) if locs else None

        parts = [description] if description else []
        if effect and effect != description:
            parts.append(effect)
        if loc_str:
            parts.append(f"Found in: {loc_str}")

        docs.append({
            "entity_type": "item",
            "name": normalized_name,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": description,
            "text_content": "\n".join(parts),
            "tags": ["Talisman"],
            "location": loc_str,
            "weight":       weight,
            "effect":       effect,
            "effect_value": effect_value,
            "base_item":    _variant_base_name(normalized_name),
            **_acquisition_fields(normalized_name, drop_map, merchant_items),
        })

    print(f"  Talisman supplement: {len(docs)} DLC entries added")
    return docs


def load_discord_bot_npcs() -> list[dict]:
    """Parse NPC profiles from the Discord bot's npcs.csv.

    Provides character profiles — name, location, role, voiced-by, description —
    distinct from npc_dialogue (which stores full conversation transcripts). Useful
    for queries like "where is Kalé?" or "which NPCs are merchants?".
    """
    print("  Downloading npcs.csv …")
    resp = requests.get(f"{DISCORD_BOT_BASE}/npcs.csv", timeout=30)
    resp.raise_for_status()

    docs: list[dict] = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        if not name:
            continue

        location = row.get("location", "").strip() or None
        role = row.get("role", "").strip()
        voiced_by_raw = row.get("voiced by", "").strip()
        voiced_by = voiced_by_raw if voiced_by_raw and "voice actor goes here" not in voiced_by_raw.lower() else ""
        description = row.get("description", "").strip()

        # Derive searchable tags from role string
        role_lower = role.lower()
        tags: list[str] = []
        if "merchant" in role_lower or "shop" in role_lower or "trader" in role_lower:
            tags.append("merchant")
        if "quest" in role_lower:
            tags.append("quest_npc")
        if "summon" in role_lower:
            tags.append("summonable")
        if "blacksmith" in role_lower:
            tags.append("blacksmith")
        if not tags:
            tags.append("npc")

        parts = [description] if description else []
        if role:
            parts.append(f"Role: {role}")
        if location:
            parts.append(f"Location: {location}")
        if voiced_by:
            parts.append(f"Voiced by: {voiced_by}")
        text_content = "\n".join(parts)

        docs.append({
            "entity_type": "npc",
            "name": name,
            "patch_version": DLC_PATCH_VERSION,
            "source": "fextralife-discord-bot",
            "description": description[:500] if description else text_content[:500],
            "text_content": text_content,
            "tags": tags,
            "location": location,
        })

    merchant_count = sum(1 for d in docs if "merchant" in d["tags"])
    print(f"  Parsed: {{'npc': {len(docs)}}} ({merchant_count} merchants)")
    return docs


# ---------------------------------------------------------------------------
# Merchants (ShopLineupParam)
# ---------------------------------------------------------------------------

# These Row Name prefixes are armor-alteration service menus, not NPC shops.
_SKIP_SHOP_NAMES: frozenset[str] = frozenset({"Alteration", "Reversion"})

# Remembrance-trade entries in ShopLineupParam aren't named after Enia;
# hard-map them to her location.
_REMEMBRANCE_LOCATION = "Roundtable Hold (Finger Reader Enia)"

# Known erdb attribution errors: (wrong_vendor, item_name) → correct_vendor.
# Iji has no entries in erdb's ShopLineupParam at all; his item is misattributed
# to Rogier (both NPCs are found in Stormveil Castle / Liurnia and share story
# connections, which likely caused the upstream data error).
_VENDOR_ITEM_OVERRIDES: dict[tuple[str, str], str] = {
    ("Sorcerer Rogier", "Carian Filigreed Crest"): "Iji",
    ("Sorcerer Rogier", "Somber Smithing Stone [1]"): "Iji",
    ("Sorcerer Rogier", "Somber Smithing Stone [2]"): "Iji",
    ("Sorcerer Rogier", "Somber Smithing Stone [3]"): "Iji",
    ("Sorcerer Rogier", "Somber Smithing Stone [4]"): "Iji",
}


def _build_merchant_location_map() -> dict[str, str]:
    """Download npcs.csv and build a merchant-name → location map.

    Uses exact match (case-insensitive) first, then prefix match so that
    "Miriel" finds "Miriel, Pastor of Vows".
    """
    resp = requests.get(f"{DISCORD_BOT_BASE}/npcs.csv", timeout=30)
    resp.raise_for_status()

    name_to_loc: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = row.get("name", "").strip()
        loc = row.get("location", "").strip()
        if name and loc:
            name_to_loc[name.lower()] = loc

    return name_to_loc


def _lookup_merchant_location(merchant: str, name_to_loc: dict[str, str]) -> str | None:
    key = merchant.lower()
    if key in name_to_loc:
        return name_to_loc[key]
    # Prefix match: NPC name starts with the merchant key + punctuation/space
    # e.g. "Miriel" matches "miriel, pastor of vows"
    for npc_name, loc in name_to_loc.items():
        if npc_name.startswith(key + ",") or npc_name.startswith(key + " "):
            return loc
    return None


def _parse_merchants(z: zipfile.ZipFile, patch_version: str, npc_loc_map: dict[str, str] | None = None) -> list[dict]:
    """Parse ShopLineupParam.csv into one merchant document per NPC vendor.

    Row Name format: "[Merchant Name] Item Name" or "[Merchant Name - Condition] Item Name"
    where Condition is a prayerbook/scroll unlock, questline phase, etc.

    All condition-gated sub-inventories are merged under the base merchant name so
    a single document covers everything that NPC can ever sell.
    """
    rows = _csv_rows(z, "ShopLineupParam.csv")

    # merchant_name → condition → [item entries]
    merchant_data: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for row in rows:
        row_name = row.get("Row Name", "").strip()
        m = re.match(r"^\[(.+?)\]\s*(.+)$", row_name)
        if not m:
            continue

        full_vendor = m.group(1).strip()
        item_name = m.group(2).strip()

        base_vendor, _, condition = full_vendor.partition(" - ")
        base_vendor = base_vendor.strip()
        condition = condition.strip()

        if base_vendor in _SKIP_SHOP_NAMES:
            continue

        price = int(float(row.get("value", 0) or 0))
        qty = int(float(row.get("sellQuantity", -1) or -1))

        merchant_data[base_vendor][condition].append({
            "item": item_name,
            "price": price,
            "qty": qty,
        })

    # Correct known erdb attribution errors
    for (wrong_vendor, item_name), correct_vendor in _VENDOR_ITEM_OVERRIDES.items():
        if wrong_vendor not in merchant_data:
            continue
        for condition in list(merchant_data[wrong_vendor].keys()):
            entries = merchant_data[wrong_vendor][condition]
            corrected = [e for e in entries if e["item"] == item_name]
            remaining = [e for e in entries if e["item"] != item_name]
            if not corrected:
                continue
            merchant_data[correct_vendor][condition].extend(corrected)
            if remaining:
                merchant_data[wrong_vendor][condition] = remaining
            else:
                del merchant_data[wrong_vendor][condition]
        if not any(merchant_data[wrong_vendor].values()):
            del merchant_data[wrong_vendor]

    docs: list[dict] = []
    for vendor_name, conditions in merchant_data.items():
        # Build readable text_content grouped by unlock condition
        text_lines: list[str] = [f"Items sold by {vendor_name}:"]
        all_items: list[str] = []

        # Unconditional items first, then condition-gated
        for condition in sorted(conditions.keys(), key=lambda c: (bool(c), c)):
            entries = conditions[condition]
            if condition:
                text_lines.append(f"\n  [Requires: {condition}]")
            for e in entries:
                qty_str = "" if e["qty"] == -1 else f" (qty: {e['qty']})"
                price_str = f"{e['price']:,}" if e["price"] > 0 else "free"
                text_lines.append(f"  {e['item']} — {price_str} runes{qty_str}")
                all_items.append(e["item"])

        total = len(all_items)
        description = f"Sells {total} item{'s' if total != 1 else ''}: " + ", ".join(all_items[:6])
        if total > 6:
            description += f" and {total - 6} more"

        # Tag by vendor type
        tags: list[str] = []
        vl = vendor_name.lower()
        if any(k in vl for k in ("merchant", "kale", "patches", "boggart", "pidia", "rogier")):
            tags.append("merchant")
        if "enia" in vl or "remembrance" in vl or "elden remembrance" in vl:
            tags.extend(["special_vendor", "remembrance_trade"])
        if "dragon communion" in vl:
            tags.extend(["special_vendor", "dragon_communion"])
        if "miriel" in vl or "corhyn" in vl or "sellen" in vl or "gowry" in vl:
            tags.append("spell_vendor")
        if not tags:
            tags.append("vendor")

        # Resolve location
        location: str | None = None
        if npc_loc_map:
            if "remembrance" in vl or "elden remembrance" in vl:
                location = _REMEMBRANCE_LOCATION
            else:
                # For generic "Merchant" (nomadic merchants grouped by location-as-condition),
                # derive location from the condition names embedded in text_content.
                if vendor_name == "Merchant" and conditions:
                    loc_parts = [c for c in conditions if c and c not in ("", "Isolated Merchant", "Abandoned Merchant")]
                    if loc_parts:
                        location = "; ".join(loc_parts[:4]) + (" …" if len(loc_parts) > 4 else "")
                else:
                    location = _lookup_merchant_location(vendor_name, npc_loc_map)

        docs.append({
            "entity_type": "merchant",
            "name": vendor_name,
            "patch_version": patch_version,
            "source": "erdb",
            "description": description,
            "text_content": "\n".join(text_lines),
            "tags": tags,
            "location": location,
        })

    return docs


# ---------------------------------------------------------------------------
# erdb top-level loader
# ---------------------------------------------------------------------------

def load_erdb(
    version: str = ERDB_DEFAULT_VERSION,
    location_map: dict[str, list[str]] | None = None,
    jp_fmgs: dict | None = None,
    npc_loc_map: dict[str, str] | None = None,
    supplement_dlc_aow: bool = True,
    supplement_dlc: bool = True,
    drop_map: dict[str, dict[str, list[str]]] | None = None,
) -> list[dict]:
    url = ERDB_ZIP_URL.format(version=version)
    print(f"  Downloading erdb {version} from GitHub …")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    patch_version = version  # e.g. "1.10.0"
    zip_data = io.BytesIO(resp.content)

    lm = location_map or {}
    print(f"  Parsing game data (patch {patch_version}) …")
    if npc_loc_map is None:
        print("  Downloading NPC location data for merchant enrichment …")
        npc_loc_map = _build_merchant_location_map()
    bot_talisman_map = _load_discord_bot_talismans()
    with zipfile.ZipFile(zip_data) as z:
        merchant_items = _extract_merchant_items(z)
        weapons   = _parse_weapons(z, patch_version, lm, jp_fmgs, drop_map, merchant_items)
        armor     = _parse_armor(z, patch_version, lm, jp_fmgs, drop_map, merchant_items)
        spells    = _parse_spells(z, patch_version, lm, jp_fmgs, drop_map, merchant_items)
        aow       = _parse_ashes_of_war(z, patch_version, lm, jp_fmgs, drop_map, merchant_items)
        talismans = _parse_talismans(z, patch_version, lm, jp_fmgs, drop_map, merchant_items, discord_bot_map=bot_talisman_map)
        merchants = _parse_merchants(z, patch_version, npc_loc_map)

    if supplement_dlc_aow or supplement_dlc:
        aow = aow + _supplement_aow(aow, location_map=lm, drop_map=drop_map, merchant_items=merchant_items)
    if supplement_dlc:
        weapons   = weapons   + _supplement_weapons(weapons, location_map=lm, drop_map=drop_map, merchant_items=merchant_items)
        armor     = armor     + _supplement_armor(armor, drop_map=drop_map, merchant_items=merchant_items)
        spells    = spells    + _supplement_spells(spells, drop_map=drop_map, merchant_items=merchant_items)
        talismans = talismans + _supplement_talismans(talismans, location_map=lm, drop_map=drop_map, merchant_items=merchant_items, discord_bot_map=bot_talisman_map)

    counts = {
        "weapons": len(weapons), "armor": len(armor), "spells": len(spells),
        "ashes_of_war": len(aow), "talismans": len(talismans),
        "merchants": len(merchants),
    }
    print(f"  Parsed: {counts}")
    if jp_fmgs:
        ja_counts = {
            "weapons": f"{_count_ja(weapons)}/{len(weapons)}",
            "armor":   f"{_count_ja(armor)}/{len(armor)}",
            "spells":  f"{_count_ja(spells)}/{len(spells)}",
            "ashes_of_war": f"{_count_ja(aow)}/{len(aow)}",
            "talismans":    f"{_count_ja(talismans)}/{len(talismans)}",
        }
        print(f"  JP coverage: {ja_counts}")
    return weapons + armor + spells + aow + talismans + merchants


# ---------------------------------------------------------------------------
# Index / bulk load
# ---------------------------------------------------------------------------

def ensure_index(client: OpenSearch, recreate: bool = False) -> None:
    if recreate and client.indices.exists(index=INDEX):
        print(f"  Deleting index '{INDEX}' for recreation …")
        client.indices.delete(index=INDEX)
    if not client.indices.exists(index=INDEX):
        print(f"  Creating index '{INDEX}' …")
        client.indices.create(index=INDEX, body=INDEX_MAPPING)
    else:
        print(f"  Index '{INDEX}' already exists, updating mapping …")
        client.indices.put_mapping(index=INDEX, body=INDEX_MAPPING["mappings"])


def _to_bulk_actions(docs: list[dict]) -> list[dict]:
    return [
        {
            "_index": INDEX,
            # Unique ID: entity_type::name::patch_version
            # Using name (not internal row ID) so the same weapon across patches
            # gets the same logical key when patch_version matches.
            "_id": f"{d['entity_type']}::{d['name']}::{d.get('patch_version', 'unknown')}",
            "_source": {k: v for k, v in d.items() if v is not None},
        }
        for d in docs
        if d.get("name")
    ]


def load_documents(client: OpenSearch | None, docs: list[dict], dry_run: bool) -> None:
    actions = _to_bulk_actions(docs)
    if not actions:
        print("  No documents to index.")
        return
    if dry_run:
        print(f"  [dry-run] Would index {len(actions)} documents.")
        print(json.dumps(actions[:2], indent=2, ensure_ascii=False))
        return
    success, errors = bulk(client, actions, raise_on_error=False, chunk_size=500)
    print(f"  Indexed {success} documents.")
    if errors:
        print(f"  {len(errors)} errors (first 3):")
        for e in errors[:3]:
            print(f"    {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Load Elden Ring data into OpenSearch.")
    parser.add_argument(
        "--erdb-version",
        default=ERDB_DEFAULT_VERSION,
        choices=ERDB_VERSIONS,
        help=f"erdb gamedata version to load (default: {ERDB_DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--all-patches",
        action="store_true",
        help=(
            "Load all available erdb patch versions (1.02.1 through 1.10.0) "
            "into the index, enabling diff_entities() queries across patches. "
            "Skips DLC supplements (no patch history available)."
        ),
    )
    parser.add_argument(
        "--dialogue",
        action="store_true",
        help="Also load NPC dialogue from fromsoft-fts.",
    )
    parser.add_argument(
        "--dialogue-only",
        action="store_true",
        help="Load NPC dialogue only, skip erdb item data.",
    )
    parser.add_argument(
        "--enemies",
        action="store_true",
        help="Also load enemy/boss data from the Discord bot CSVs.",
    )
    parser.add_argument(
        "--enemies-only",
        action="store_true",
        help="Load enemy data only, skip erdb item data.",
    )
    parser.add_argument(
        "--npcs",
        action="store_true",
        help="Also load NPC profiles from the Discord bot npcs.csv.",
    )
    parser.add_argument(
        "--npcs-only",
        action="store_true",
        help="Load NPC profiles only, skip erdb item data.",
    )
    parser.add_argument(
        "--locations",
        action="store_true",
        help="Enrich item documents with location data from locations.csv (requires a full erdb reload).",
    )
    parser.add_argument(
        "--recreate-index",
        action="store_true",
        help=(
            "Delete and recreate the OpenSearch index before loading. "
            "Required when the index settings (e.g. custom analyzers) have changed, "
            "since OpenSearch does not allow updating settings on an existing index."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and normalize but don't write to OpenSearch.",
    )
    parser.add_argument(
        "--no-japanese",
        action="store_true",
        help="Skip Japanese text enrichment (skips download of JP FMG data).",
    )
    args = parser.parse_args()

    skip_erdb = args.dialogue_only or args.enemies_only or args.npcs_only

    location_map: dict[str, list[str]] | None = None
    if args.locations and not skip_erdb:
        print("Loading item location map …")
        location_map = load_location_map()

    jp_fmgs: dict | None = None
    if not args.no_japanese:
        print("Loading Japanese FMG data …")
        jp_fmgs = _load_jp_fmgs()

    docs: list[dict] = []

    if not skip_erdb:
        versions_to_load = ERDB_VERSIONS if args.all_patches else [args.erdb_version]
        # Download shared lookup tables once and reuse across all patch versions.
        print("  Downloading NPC location data for merchant enrichment …")
        npc_loc_map = _build_merchant_location_map()
        print("Loading acquisition drop map …")
        drop_map = _build_drop_map()
        for i, version in enumerate(versions_to_load):
            label = f"({i + 1}/{len(versions_to_load)})" if len(versions_to_load) > 1 else ""
            print(f"Loading erdb {version} {label}…")
            docs += load_erdb(
                version,
                location_map=location_map,
                jp_fmgs=jp_fmgs,
                npc_loc_map=npc_loc_map,
                # DLC supplements have no patch history; only add them once on the
                # default (latest) erdb load, not when loading all historical patches.
                supplement_dlc=not args.all_patches,
                supplement_dlc_aow=not args.all_patches,
                drop_map=drop_map,
            )

    if args.dialogue or args.dialogue_only:
        print("Loading NPC dialogue …")
        docs += load_fromsoft_fts(jp_fmgs=jp_fmgs)

    if args.enemies or args.enemies_only:
        print("Loading enemy data …")
        docs += load_discord_bot_enemies(jp_fmgs=jp_fmgs)

    if args.npcs or args.npcs_only:
        print("Loading NPC profiles …")
        docs += load_discord_bot_npcs()

    print(f"Total documents: {len(docs)}")

    if args.dry_run:
        load_documents(None, docs, dry_run=True)  # type: ignore[arg-type]
        return

    client = _get_client()
    ensure_index(client, recreate=args.recreate_index)
    load_documents(client, docs, dry_run=False)


if __name__ == "__main__":
    main()
