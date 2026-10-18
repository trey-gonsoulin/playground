"""Shared mapping fragments and one ensure_index (#459)."""

import hashlib
import json

import elden_ring._calc as _calc
import elden_ring._client as _client

# sha256 of json.dumps(INDEX_MAPPING, sort_keys=True) before the #459 dedupe
# (main @ a3cd4e8). The refactor must not change a byte of the mapping. If you
_MAPPING_SHA256 = "4f56a2059fcb98e42d36fe7b2ef9cb0d9df21857cf5da622064b6f014433ee9a"

_PROPS = _client.INDEX_MAPPING["mappings"]["properties"]


def test_mapping_unchanged_by_dedupe():
    s = json.dumps(_client.INDEX_MAPPING, sort_keys=True)
    assert hashlib.sha256(s.encode()).hexdigest() == _MAPPING_SHA256


def test_ja_text_fragment():
    for field in (
        "subtitles_ja",
        "name_ja",
        "description_ja",
        "text_content_ja",
        "effect_text_ja",
        "info_text_ja",
    ):
        assert _PROPS[field] is _client._JA_TEXT, field
    analyzers = {k: v["analyzer"] for k, v in _client._JA_TEXT["fields"].items()}
    assert set(analyzers.values()) <= set(
        _client.INDEX_MAPPING["settings"]["analysis"]["analyzer"]
    )


def test_skill_attacks_by_hand_fragment():
    # #500: shield arts' per-hand hit lists, on the skill and per class
    skill = _PROPS["skill"]["properties"]
    by_hand = _client._SKILL_ATTACKS_BY_HAND_MAPPING
    assert skill["attacks_by_hand"] is by_hand
    assert skill["attacks_by_class"]["properties"]["attacks_by_hand"] is by_hand
    assert by_hand["properties"]["hands"] == {"type": "keyword"}
    assert by_hand["properties"]["attacks"] is _client._SKILL_ATTACKS_MAPPING


def test_vocabulary_from_calc():
    assert _client._DAMAGE_TYPES is _calc.DAMAGE_TYPES
    assert _client.STATS is _calc.STATS
    assert not hasattr(_client, "_STATS")
    assert set(_client._AP_INT["properties"]) == set(_calc.DAMAGE_TYPES)


def test_single_ensure_index():
    # opensearch_load.ensure_index (create + limit + put_mapping) is the only one.
    assert not hasattr(_client, "ensure_index")
