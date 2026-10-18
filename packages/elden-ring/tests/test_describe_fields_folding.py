"""describe_fields folds repeated object subtrees and filters by prefix (#452)."""

import json

from elden_ring import _client
from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING, _mapping_paths

_PATHS = _mapping_paths(INDEX_MAPPING["mappings"]["properties"])


class _C:
    def search(self, index, body):
        return {"aggregations": {"types": {"buckets": []}, "sources": {"buckets": []}}}


def _describe(**kw) -> dict:
    return _client.describe_index(_C(), **kw)


def test_folded_catalog_expands_back_to_the_full_one():
    full = _describe(full=True)["fields"]
    folded = _describe()["fields"]
    roots = {p: e for p, e in folded.items() if "same_shape_as" in e}
    assert roots and len(folded) < len(full) // 2
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
    folded = _describe()["fields"]
    roots = {p: e for p, e in folded.items() if "same_shape_as" in e}
    for root, entry in roots.items():
        canon = entry["same_shape_as"]
        assert canon in folded and "same_shape_as" not in folded[canon], root
        assert not any(canon.startswith(r + ".") for r in roots), root
        n = sum(1 for p in _PATHS if p.startswith(canon + "."))
        assert entry["folded_paths"] == n, root
        assert canon.count(".") <= root.count("."), root


def test_shared_fragments_fold_onto_their_shallowest_path():
    folded = _describe()["fields"]
    assert folded["max_level.summon_stats"]["same_shape_as"] == "summon_stats"
    set_when = {
        folded[p]["same_shape_as"]
        for p in folded
        if p.endswith("set_when") and "same_shape_as" in folded[p]
    }
    assert len(set_when) == 1
    assert "gate_set_when" in folded and "gate_set_when.flag" not in folded
    # Small repeats ({x, y, z}) stay listed.
    assert "same_shape_as" not in folded["world_position"]


def test_folding_cuts_the_payload():
    full = len(json.dumps(_describe(full=True)))
    folded = _describe()
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
