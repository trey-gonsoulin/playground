"""Shared mapping fragments and one ensure_index (#459)."""

import hashlib
import json

import elden_ring._calc as _calc
import elden_ring._client as _client
import elden_ring._schema as _schema

# sha256 of json.dumps(INDEX_MAPPING, sort_keys=True) before the #459 dedupe
# (main @ a3cd4e8). The refactor must not change a byte of the mapping. If you
_MAPPING_SHA256 = "caa61637458d7e2274a5682fda5be342dda88de5429de14ee9a6d53b8dff5f47"

_PROPS = _schema.INDEX_MAPPING["mappings"]["properties"]


def test_mapping_unchanged_by_dedupe():
    s = json.dumps(_schema.INDEX_MAPPING, sort_keys=True)
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
        assert _PROPS[field] is _schema._JA_TEXT, field
    analyzers = {k: v["analyzer"] for k, v in _schema._JA_TEXT["fields"].items()}
    assert set(analyzers.values()) <= set(
        _schema.INDEX_MAPPING["settings"]["analysis"]["analyzer"]
    )


def test_vocabulary_from_calc():
    assert _schema._DAMAGE_TYPES is _calc.DAMAGE_TYPES
    assert _schema.STATS is _calc.STATS
    assert not hasattr(_schema, "_STATS")
    assert set(_schema._AP_INT["properties"]) == set(_calc.DAMAGE_TYPES)


def test_single_ensure_index():
    # opensearch_load.ensure_index (create + limit + put_mapping) is the only one.
    assert not hasattr(_client, "ensure_index")
