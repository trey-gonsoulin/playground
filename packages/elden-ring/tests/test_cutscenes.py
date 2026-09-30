"""Cutscene catalog (#92): the cutscene doc fields and the boss back-reference are
mapped and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# A 1.17 build's Margit intro doc, trimmed; every leaf must be mapped (strict).
_DOC = {
    "entity_type": "cutscene",
    "name": "Cutscene 10000010",
    "patch_version": "1.17.0",
    "source": "EMEVD",
    "cutscene_id": 10000010,
    "label": "Margit, the Fell Omen: boss intro",
    "map": "m10_00_00_00",
    "trigger_kind": "boss_intro",
    "is_ending": False,
    "unskippable": False,
    "tags": ["cutscene"],
    "variant_ids": [10000011],
    "asset": "s10_00_0010",
    "boss": "Margit, the Fell Omen",
    "trigger_flags": [10002855],
    "trigger_items": ["Dectus Medallion (Left)"],
    "warp_region": 10002852,
    "subtitles": ["Foul Tarnished,", "In search of the Elden Ring."],
    "subtitles_ja": ["褪せ人よ", "エルデンリングを求める"],
    "talk_ids": [20030000, 20030100],
    "text_content": "Margit, the Fell Omen: boss intro\nFoul Tarnished,",
    "text_content_ja": "褪せ人よ",
}


def test_cutscene_doc_fields_mapped():
    for field in _DOC:
        assert field in _PROPS, field
    assert _PROPS["cutscene_id"] == {"type": "long"}
    assert _PROPS["trigger_kind"] == {"type": "keyword"}
    assert _PROPS["subtitles"] == {"type": "text"}
    assert _PROPS["subtitles_ja"]["fields"]["ja"]["analyzer"] == "kuromoji_analyzer"
    assert _PROPS["is_ending"] == {"type": "boolean"}


def test_boss_cutscenes_mapped():
    sub = _PROPS["cutscenes"]["properties"]
    assert sub == {"id": {"type": "long"}, "kind": {"type": "keyword"}}


def test_cutscene_fields_documented():
    for field in (
        "cutscene_id",
        "variant_ids",
        "asset",
        "label",
        "trigger_kind",
        "boss",
        "trigger_flags",
        "trigger_items",
        "warp_region",
        "is_ending",
        "unskippable",
        "subtitles",
        "subtitles_ja",
        "talk_ids",
        "cutscenes",
    ):
        assert field in _FIELD_NOTES, field
    assert "cutscene" in _FIELD_NOTES["entity_type"]
    assert "Dialogue <id>" in _FIELD_NOTES["talk_ids"]
    assert "boss_intro" in _FIELD_NOTES["trigger_kind"]
    assert "EMEVD" in _FIELD_NOTES["source"]
