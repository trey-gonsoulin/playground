"""Item pickups just outside a landmark (#427) and banner-area location docs (#428).

No OpenSearch needed.
"""

from elden_ring import _client
from elden_ring._client import _FIELD_NOTES, _with_drop_fields


def test_near_locations_is_mapped_and_noted():
    props = _client.INDEX_MAPPING["mappings"]["properties"]
    assert props["near_locations"] == {"type": "keyword"}
    note = _FIELD_NOTES["near_locations"]
    assert "#427" in note and "50 m" in note
    assert "near_location" in _FIELD_NOTES["placements"]


def test_near_locations_is_not_a_locations_twin():
    # a pickup merely near a landmark isn't in it: locations stays precise
    assert "near_locations" not in _with_drop_fields(["locations"])


def test_banner_area_kind_is_noted():
    assert "#428" in _FIELD_NOTES["kind"]
    assert "MapNameOverride" in _FIELD_NOTES["kind"]
    assert "#428" in _FIELD_NOTES["footprint"]


def test_near_items_and_banner_contents_are_noted():
    # #476: a location's near pickups, and what a banner area holds
    props = _client.INDEX_MAPPING["mappings"]["properties"]
    assert props["near_items"] == {"type": "keyword"}
    assert "#476" in _FIELD_NOTES["near_items"]
    assert "near_location" in _FIELD_NOTES["near_items"]
    assert "near_items" in _FIELD_NOTES["items"]
    assert "MapNameOverride" in _FIELD_NOTES["items"]
    assert "#476" in _FIELD_NOTES["enemies"]
    assert "near_items" not in _with_drop_fields(["items"])


def test_map_event_and_action_button_sites_are_noted():
    # #429: those game_text docs reuse the placement summary fields
    for f in ("placements", "maps", "regions", "areas", "locations"):
        assert "#429" in _FIELD_NOTES[f], f
        assert "action_button" in _FIELD_NOTES[f], f
