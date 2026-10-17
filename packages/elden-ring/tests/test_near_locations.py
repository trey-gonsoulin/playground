"""Item pickups just outside a landmark (#427) and banner-area location docs (#428).

No OpenSearch needed.
"""

from elden_ring import _client
from elden_ring._client import _FIELD_NOTES, _with_drop_fields


def test_near_locations_is_mapped_and_noted():
    props = _client.INDEX_MAPPING["mappings"]["properties"]
    assert props["near_locations"] == {"type": "keyword"}
    assert "near_location" in _FIELD_NOTES["placements"]


def test_near_locations_is_not_a_locations_twin():
    # a pickup merely near a landmark isn't in it: locations stays precise
    assert "near_locations" not in _with_drop_fields(["locations"])
