"""#164: enemy / summon attacks narrowed by the AI battle scripts (no new fields).

Wording is not pinned (#460): these check that the affected notes exist and name
the field they cross-reference.
"""

from elden_ring._client import _queryable_paths
from elden_ring._field_notes import _FIELD_NOTES


def test_ai_narrowed_attack_fields_are_documented():
    for key in (
        "attacks",
        "attacks.state_variants",
        "attacks.state_variants.special_states",
        "summon_stats.attacks",
        "attacks.parryable",
        "attacks.parryable_animations",
        "grabs",
        "summon_stats.weapons",
    ):
        assert key in _FIELD_NOTES, key


def test_parry_and_grabs_notes_point_at_state_variants():
    assert "attacks.state_variants" in _queryable_paths()
    assert "attacks.state_variants" in _FIELD_NOTES["attacks.parryable"]
    assert "attacks.state_variants" in _FIELD_NOTES["grabs"]
