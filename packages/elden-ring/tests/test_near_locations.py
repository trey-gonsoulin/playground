"""Item pickups just outside a landmark (#427) and banner-area location docs (#428).

No OpenSearch needed.
"""

from elden_ring import _schema
from elden_ring._client import _with_drop_fields
from elden_ring._field_notes import _FIELD_NOTES


def test_near_locations_is_mapped_and_noted():
    props = _schema.INDEX_MAPPING["mappings"]["properties"]
    assert props["near_locations"] == {"type": "keyword"}
    assert "near_location" in _FIELD_NOTES["placements"]


def test_near_locations_is_not_a_locations_twin():
    # a pickup merely near a landmark isn't in it: locations stays precise
    assert "near_locations" not in _with_drop_fields(["locations"])


def test_near_items_is_mapped_and_noted():
    # #476: a location's near pickups (contract facts only, #471)
    props = _schema.INDEX_MAPPING["mappings"]["properties"]
    assert props["near_items"] == {"type": "keyword"}
    assert "near_items" in _FIELD_NOTES
    assert "near_items" not in _with_drop_fields(["items"])
