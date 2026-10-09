"""Dialogue speakers, talk contexts and talk-menu options (#307): the new fields
are mapped (strict index) and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# A 1.17 build's npc_dialogue and talk_option docs, trimmed.
_LINE = {
    "entity_type": "npc_dialogue",
    "name": "Dialogue 321010040",
    "patch_version": "1.17.0",
    "source": "TalkMsg",
    "text_content": "You might have heard of me. Kenneth Haight.",
    "tags": ["dialogue"],
    "speakers": ["Kenneth Haight, Limgrave Heir"],
    "talk_contexts": ["talk"],
}
_OPTION = {
    "entity_type": "game_text",
    "name": "Talk Option 15000380",
    "patch_version": "1.17.0",
    "source": "EventTextForTalk",
    "text_content": "Increase amount replenished by flasks",
    "tags": ["talk_option"],
    "npcs": ["Site of Grace"],
}


def test_fields_mapped():
    for doc in (_LINE, _OPTION):
        for field in doc:
            assert field in _PROPS, field
    assert _PROPS["talk_contexts"] == {"type": "keyword"}
    assert _PROPS["npcs"] == {"type": "keyword"}
    assert _PROPS["speakers"] == {"type": "keyword"}


def test_fields_documented():
    assert "npc_dialogue" in _FIELD_NOTES["speakers"]
    assert "Kenneth Haight" in _FIELD_NOTES["speakers"]
    for ctx in ("talk", "nearby", "attacked", "hostile", "killed", "player_killed"):
        assert ctx in _FIELD_NOTES["talk_contexts"], ctx
    assert "talk_option" in _FIELD_NOTES["npcs"]
    assert "talk_option" in _FIELD_NOTES["tags"]
