"""Nearby-status triggers (#170) and chained conditions / durations (#158): they reuse
the effects mapping (condition, duration) and the notes document them. No OpenSearch
needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# Kindred of Rot's Exultation and a grease's buildup, as the builder emits them.
_ENTRIES = [
    {
        "stat": "attack",
        "value": 20.0,
        "unit": "%",
        "pvp_value": 20.0,
        "target": "self",
        "duration": 20.0,
        "condition": "when poison or rot occurs within 7m",
    },
    {
        "stat": "hemorrhage buildup",
        "value": 30.0,
        "unit": "points",
        "target": "enemy",
        "duration": 60.0,
    },
]


def test_trigger_fields_mapped():
    # dynamic:strict: no new leaves, the trigger rides on condition / duration.
    leaves = _PROPS["effects"]["properties"]
    for entry in _ENTRIES:
        for key in entry:
            assert key in leaves, key


def test_triggers_documented():
    note = _FIELD_NOTES["effects"]
    assert "occurs within 7m" in note and "#170" in note
    assert "until hit" in note and "non-physical damage" in note
    assert "grease" in note and "some trigger conditions" not in note
