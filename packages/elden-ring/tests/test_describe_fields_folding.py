"""describe_fields: top-level default, prefix filter and allowlisted folds (#452, #492)."""

import json

from elden_ring import _client
from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import INDEX_MAPPING, _mapping_paths

_PROPS = INDEX_MAPPING["mappings"]["properties"]
_PATHS = _mapping_paths(_PROPS)


class _C:
    def search(self, index, body):
        return {"aggregations": {"types": {"buckets": []}, "sources": {"buckets": []}}}


def _describe(**kw) -> dict:
    return _client.describe_index(_C(), **kw)


def _folded_catalog() -> dict:
    """The whole catalog, folded: every top-level field as a prefix."""
    return _describe(prefix=list(_PROPS))


def test_folded_catalog_expands_back_to_the_full_one():
    full = _describe(full=True)["fields"]
    folded = _folded_catalog()["fields"]
    roots = {p: e for p, e in folded.items() if "same_shape_as" in e}
    assert roots and len(folded) < 0.6 * len(full)
    rebuilt = {}
    for path in _PATHS:
        root = next((r for r in roots if path.startswith(r + ".")), None)
        if root is None:
            entry = dict(folded[path])
            entry.pop("same_shape_as", None)
            entry.pop("folded_paths", None)
        else:
            twin = roots[root]["same_shape_as"] + path[len(root) :]
            entry = dict(folded.get(path) or full[twin])
            entry.pop("note", None)
            if path in _FIELD_NOTES:
                entry["note"] = folded.get(path, {}).get("note", full[twin].get("note"))
        rebuilt[path] = entry
    assert rebuilt == full


def test_fold_roots_name_a_listed_canonical_and_count_its_paths():
    folded = _folded_catalog()["fields"]
    roots = {p: e for p, e in folded.items() if "same_shape_as" in e}
    for root, entry in roots.items():
        canon = entry["same_shape_as"]
        assert canon in folded and "same_shape_as" not in folded[canon], root
        assert not any(canon.startswith(r + ".") for r in roots), root
        n = sum(1 for p in _PATHS if p.startswith(canon + "."))
        assert entry["folded_paths"] == n, root
        assert canon.count(".") <= root.count("."), root


def test_shared_fragments_fold_onto_their_shallowest_path():
    folded = _folded_catalog()["fields"]
    assert folded["max_level.summon_stats"]["same_shape_as"] == "summon_stats"
    set_when = {
        folded[p]["same_shape_as"]
        for p in folded
        if p.endswith("set_when") and "same_shape_as" in folded[p]
    }
    assert len(set_when) == 1
    root = "npc_summons.requires_set_when"
    assert root in folded and f"{root}.flag" not in folded
    # A top-level copy named as a prefix is listed in full.
    assert "gate_set_when.flag" in folded
    assert folded["outcomes.when"]["same_shape_as"] == "steps.when"
    assert folded["phases.resistances"]["same_shape_as"] == "resistances"
    # Repeats outside the allowlist ({x, y, z}) stay listed.
    assert "same_shape_as" not in folded["world_position"]


def test_fields_that_only_share_keys_do_not_fold():
    """#492: equal shapes with different meanings stay separate rows."""
    folded = _folded_catalog()["fields"]
    for path in (
        "resistances",
        "guard.resistances",
        "scripted_damage_taken.multiplier",
        "summon_stats.damage_taken_multiplier",
        "skill_shots.motion_values",
        "spell_attacks.charged",
        "to",
    ):
        assert "same_shape_as" not in folded[path], path
        assert any(p.startswith(path + ".") for p in folded), path


def test_every_fold_stays_in_one_family():
    families = _client._fold_families(_PROPS)
    for root, (canon, _) in _client._folded_copies(_PROPS).items():
        assert families[root] == families[canon], root


def test_fold_families_name_mapped_objects():
    for family in _client._FOLD_FAMILIES:
        for path in family:
            assert "properties" in _PATHS.get(path, {}), path
    families = _client._fold_families(_PROPS)
    assert families["gate_set_when"] == families["unlock_set_when"]
    assert families["steps.when"] == families["outcomes.when"]


def test_default_lists_top_level_fields_only():
    out = _describe()
    fields = out["fields"]
    assert list(fields) == list(_PROPS)
    assert "folded" not in out and "warnings" not in out
    assert "prefix=" in out["listing"] and "full=True" in out["listing"]
    summon = fields["summon_stats"]
    assert summon["type"] == "object"
    assert summon["nested_fields"] == sum(
        1 for p in _PATHS if p.startswith("summon_stats.")
    )
    assert "nested_fields" not in fields["name"]
    assert fields["name_ja"]["subfields"] == ["ja", "morph", "lemma"]
    for name, entry in fields.items():
        if name in _FIELD_NOTES:
            assert len(entry["note"]) <= _client._SHORT_NOTE_CHARS, name
            assert not entry["note"].startswith("not searchable;"), name
    # ~37 KB against ~250 KB for the old folded default (#492).
    assert len(json.dumps(out)) < 0.15 * len(json.dumps(_describe(full=True)))


def test_short_note_cuts_at_the_first_clause():
    short = _client._short_note
    assert short("in-game sort index; lower sorts first") == "in-game sort index"
    assert short("a thing (e.g. this). More") == "a thing (e.g. this)"
    assert short("not searchable; returned by get_entity. {x, y, z}") == "{x, y, z}"
    long = short("word " * 40)
    assert len(long) <= _client._SHORT_NOTE_CHARS and long.endswith("...")


def test_folding_cuts_the_payload():
    full = len(json.dumps(_describe(full=True)))
    folded = _folded_catalog()
    assert len(json.dumps(folded)) < 0.8 * full
    assert "same_shape_as" in folded["folded"]
    assert "folded" not in _describe(full=True)


def test_prefix_lists_one_subtree_and_unfolds_it():
    out = _describe(prefix="gate_set_when")
    want = {p for p in _PATHS if p == "gate_set_when" or p.startswith("gate_set_when.")}
    assert set(out["fields"]) == want
    assert not any("same_shape_as" in e for e in out["fields"].values())
    assert "folded" not in out and "warnings" not in out
    assert _describe(prefix="gate_set_when.*")["fields"] == out["fields"]


def test_prefix_inside_a_fold_and_list_prefixes():
    out = _describe(prefix=["max_level.summon_stats.stats", "skill.name"])
    assert set(out["fields"]) == {
        p
        for p in _PATHS
        if p == "skill.name"
        or p == "max_level.summon_stats.stats"
        or p.startswith("max_level.summon_stats.stats.")
    }
    # A prefix above a fold keeps it folded.
    out = _describe(prefix="max_level")
    assert out["fields"]["max_level.summon_stats"]["same_shape_as"] == "summon_stats"


def test_unknown_prefix_warns():
    out = _describe(prefix="nmae")
    assert out["fields"] == {}
    assert out["warnings"] == ["no mapped field matches prefix 'nmae'"]
