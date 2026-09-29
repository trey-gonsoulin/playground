"""Item effect / info FMG text fields (#90): mapping, search routing, field notes."""

from elden_ring._client import (
    _FIELD_NOTES,
    _JP_BASE_FIELDS,
    _LITERAL_FIELDS,
    _LITERAL_FIELDS_LEMMA,
    _LITERAL_FIELDS_MORPH,
    _route_literal_fields,
    INDEX_MAPPING,
)

_PROPS = INDEX_MAPPING["mappings"]["properties"]
_NEW = ("effect_text", "info_text")


def test_fields_mapped_as_text_with_jp_analyzers():
    for f in _NEW:
        assert _PROPS[f] == {"type": "text"}
        ja = _PROPS[f"{f}_ja"]
        assert ja["type"] == "text"
        assert ja["fields"] == _PROPS["description_ja"]["fields"]


def test_default_literal_fields_cover_new_text():
    for f in _NEW:
        assert f in _LITERAL_FIELDS
        assert f in _LITERAL_FIELDS_MORPH and f in _LITERAL_FIELDS_LEMMA
        assert f"{f}_ja" in _LITERAL_FIELDS
        assert f"{f}_ja.morph" in _LITERAL_FIELDS_MORPH
        assert f"{f}_ja.lemma" in _LITERAL_FIELDS_LEMMA
        assert f"{f}_ja" in _JP_BASE_FIELDS


def test_explicit_jp_fields_route_to_analyzer_subfield():
    assert _route_literal_fields(["effect_text_ja", "effect_text"], True, False) == [
        "effect_text_ja.lemma",
        "effect_text",
    ]
    assert _route_literal_fields(["info_text_ja.lemma"], False, True) == [
        "info_text_ja.morph"
    ]


def test_field_notes():
    assert "WeaponEffect" in _FIELD_NOTES["effect_text"]
    assert "status_buildup" in _FIELD_NOTES["effect_text"]
    assert "ProtectorInfo" in _FIELD_NOTES["info_text"]
    assert "mountWepTextId" in _FIELD_NOTES["info_text"]  # Ash of War (#169)
    assert "item_dialog" in _FIELD_NOTES["tags"]
    for f in _NEW:
        assert f"{f}_ja" in _FIELD_NOTES
