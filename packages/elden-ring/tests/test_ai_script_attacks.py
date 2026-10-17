"""#164: enemy / summon attacks narrowed by the AI battle scripts (no new fields)."""

from elden_ring._client import _FIELD_NOTES


def test_attacks_note_describes_ai_narrowing():
    note = _FIELD_NOTES["attacks"]
    assert "#164" in note and "Frenzied" in note
    assert "never-weighted" in note and "phases count as one enemy" in note
    assert "Moves the AI alone chooses between stay" not in note


def test_state_variants_cover_ai_script_variants():
    assert "AI script" in _FIELD_NOTES["attacks.state_variants"]
    assert "AI script" in _FIELD_NOTES["attacks.state_variants.special_states"]


def test_summon_attacks_note():
    note = _FIELD_NOTES["summon_stats.attacks"]
    assert "AI script requests (#164" in note
    assert "AI-only sibling moves can remain" not in note


def test_parry_grabs_and_weapon_labels_survive_narrowing():
    parry = _FIELD_NOTES["attacks.parryable"]
    assert "attacks.state_variants" in parry and "Marionette Soldier" in parry
    assert "Margit 40 of 73" in _FIELD_NOTES["attacks.parryable_animations"]
    assert "attacks.state_variants included, #164" in _FIELD_NOTES["grabs"]
    weapons = _FIELD_NOTES["summon_stats.weapons"]
    assert "attacks aside" in weapons and "Radahn Soldier Ashes" in weapons
