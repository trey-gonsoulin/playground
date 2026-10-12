"""#292 use costs and #296 inventory/economy fields: mapping + describe_fields notes."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_use_cost_fields_mapped_and_documented():
    assert _PROPS["fp_cost"] == {"type": "integer"}
    assert _PROPS["hp_cost"] == {"type": "integer"}
    assert "Hilde 116" in _FIELD_NOTES["fp_cost"]
    assert "Bloodfiend Hexer's Ashes 500" in _FIELD_NOTES["hp_cost"]


def test_inventory_group_mapped():
    assert _PROPS["rarity"] == {"type": "integer"}
    inv = _PROPS["inventory"]["properties"]
    for leaf in ("sell_value", "max_held", "max_stored"):
        assert inv[leaf] == {"type": "integer"}, leaf
    for leaf in ("sellable", "storable", "tradable"):
        assert inv[leaf] == {"type": "boolean"}, leaf


def test_inventory_notes():
    note = _FIELD_NOTES["inventory"]
    for leaf in ("sell_value", "max_held", "max_stored", "storable", "tradable"):
        assert leaf in note, leaf
    assert "can't be sold" in note
    assert "0-3" in _FIELD_NOTES["rarity"]


def test_param_and_icon_ids_mapped_and_documented():  # #327
    assert _PROPS["param_id"] == {"type": "integer"}
    assert _PROPS["icon_id"] == {"type": "integer"}
    assert "Paramdex" in _FIELD_NOTES["param_id"]
    assert "iconId" in _FIELD_NOTES["icon_id"]
    assert _PROPS["icon_id_body_type_2"] == {"type": "integer"}
    note = _FIELD_NOTES["icon_id_body_type_2"]
    assert "body type 2" in note and "iconIdF" in note
    assert "body type 1" in _FIELD_NOTES["icon_id"]
    for n in ("icon_id", "icon_id_body_type_2"):
        assert "male" not in _FIELD_NOTES[n].lower(), n
