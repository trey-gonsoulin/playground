"""Boss location area fallback (#329): location_kind mapped and documented.
No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_location_kind_mapped():
    assert _PROPS["location_kind"] == {"type": "keyword"}


def test_location_notes_describe_area_fallback():
    kind = _FIELD_NOTES["location_kind"]
    assert "'landmark'" in kind and "'area'" in kind and "#329" in kind
    loc = _FIELD_NOTES["location"]
    assert "location_kind 'area'" in loc and "region map" in loc


def test_search_docstring_mentions_location_kind():
    doc = mcp_server.search_entities.__doc__ or ""
    assert "location_kind" in doc and "location_type" in doc


def test_location_type_mapped_and_documented():
    # #340: the landmark's marker kind, kept apart from location_kind
    assert _PROPS["location_type"] == {"type": "keyword"}
    note = _FIELD_NOTES["location_type"]
    assert "#340" in note and "'landmark'" in note and "'area'" in note
    for kind in ("catacombs", "cave", "evergaol", "minor_erdtree", "legacy_dungeon"):
        assert kind in note


def test_separate_map_arena_location_documented():
    # #339: separate-map arenas take their entrance marker's location
    loc = _FIELD_NOTES["location"]
    assert "#339" in loc and "WorldMapLegacyConvParam" in loc
    assert "Grand Cloister" in loc and "Absent only" not in loc


def test_marker_icon_kinds_documented():
    # #346: the newly named marker icons are listed for both location kind and
    # boss location_type
    for field in ("kind", "location_type"):
        note = _FIELD_NOTES[field]
        assert "#346" in note
        for kind in ("eternal_city", "underground_ruins", "sunken_town", "bridge"):
            assert kind in note, (field, kind)
