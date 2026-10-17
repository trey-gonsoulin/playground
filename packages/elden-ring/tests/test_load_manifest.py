"""Incremental loads by content-hash manifest (#409). No OpenSearch needed."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import load_dataset  # noqa: E402
import load_manifest  # noqa: E402


def _doc(name, etype="weapon", version="1.05", **kw):
    return {"entity_type": etype, "name": name, "patch_version": version, **kw}


def _id(d):
    return f"{d['entity_type']}::{d['name']}::{d['patch_version']}"


class _Live:
    """Live ids per index for the stubbed scan; loads add to it, as indexing does."""

    def __init__(self):
        self.ids: set[str] = set()
        self.scans = 0

    def scan(self, client, index, _source, query):
        self.scans += 1
        f = query["query"]["bool"]["filter"]
        version, types = f[0]["term"]["patch_version"], f[1]["terms"]["entity_type"]
        for i in sorted(self.ids):
            t, _, v = i.split("::")
            if v == version and t in types:
                yield {"_id": i}


@pytest.fixture
def live(monkeypatch):
    lv = _Live()
    monkeypatch.setattr(load_manifest, "scan", lv.scan)
    return lv


def _load(monkeypatch, tmp_path, live, docs, *flags, indices=("er-v2",), errors=()):
    """Run load_dataset.main on ``docs``; returns the names it indexed."""
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps(docs))
    loaded = []

    def fake_load(client, docs, dry_run):
        loaded.extend(d["name"] for d in docs)
        if not dry_run and not errors:
            live.ids |= {_id(d) for d in docs}
        return len(docs), list(errors)

    monkeypatch.setattr(load_dataset, "_get_client", lambda: object())
    monkeypatch.setattr(load_dataset, "ensure_index", lambda c, recreate: None)
    monkeypatch.setattr(load_dataset, "load_documents", fake_load)
    monkeypatch.setattr(
        load_dataset, "prune_stale", lambda c, docs, dry_run, **kw: (0, [])
    )
    monkeypatch.setattr(load_manifest, "_concrete_indices", lambda c: list(indices))
    monkeypatch.setattr(sys, "argv", ["load_dataset.py", str(ds), *flags])
    load_dataset.main()
    return loaded


def test_rerun_loads_only_changed_docs(monkeypatch, tmp_path, live):
    m = ["--manifest-dir", str(tmp_path / "m"), "--prune"]
    docs = [
        _doc("Dagger", weight=1),
        _doc("Club", weight=2),
        _doc("Melina 1", "npc_dialogue"),
    ]
    assert _load(monkeypatch, tmp_path, live, docs, *m) == [
        "Dagger",
        "Club",
        "Melina 1",
    ]
    assert live.scans == 0  # no manifest yet: nothing to check against
    assert _load(monkeypatch, tmp_path, live, docs, *m) == []  # nothing changed
    docs[1] = _doc("Club", weight=3)  # changed content
    docs.append(_doc("Mace"))  # new doc
    assert _load(monkeypatch, tmp_path, live, docs, *m) == ["Club", "Mace"]
    assert _load(monkeypatch, tmp_path, live, docs, *m) == []
    # key order and None values don't count as a change (None isn't indexed)
    docs[0] = {
        "weight": 1,
        "name": "Dagger",
        "patch_version": "1.05",
        "entity_type": "weapon",
        "extra": None,
    }
    assert _load(monkeypatch, tmp_path, live, docs, *m) == []


def test_deleted_live_doc_is_reloaded(monkeypatch, tmp_path, live):
    m = ["--manifest-dir", str(tmp_path / "m")]
    docs = [_doc("Dagger"), _doc("Club")]
    _load(monkeypatch, tmp_path, live, docs, *m)
    live.ids.discard(_id(docs[0]))  # e.g. pruned by another load
    assert _load(monkeypatch, tmp_path, live, docs, *m) == ["Dagger"]


def test_full_new_index_and_failed_load(monkeypatch, tmp_path, live):
    m = ["--manifest-dir", str(tmp_path / "m")]
    docs = [_doc("Dagger"), _doc("Club")]
    _load(monkeypatch, tmp_path, live, docs, *m)
    assert _load(monkeypatch, tmp_path, live, docs, *m, "--full") == ["Dagger", "Club"]
    # an alias swap to a new concrete index has no manifest: full load
    assert _load(monkeypatch, tmp_path, live, docs, *m, indices=("er-v3",)) == [
        "Dagger",
        "Club",
    ]
    assert _load(monkeypatch, tmp_path, live, docs, *m, indices=("er-v3",)) == []
    # two concrete indices behind the alias: full load, no manifest written
    assert len(_load(monkeypatch, tmp_path, live, docs, *m, indices=("a", "b"))) == 2
    assert not (tmp_path / "m/a").exists()
    # a failed load drops its types from the manifest: they load in full next time
    docs[0] = _doc("Dagger", weight=9)
    with pytest.raises(SystemExit):
        _load(monkeypatch, tmp_path, live, docs, *m, errors=[{"index": {}}])
    assert _load(monkeypatch, tmp_path, live, docs, *m) == ["Dagger", "Club"]


def test_failed_load_then_revert_reloads(monkeypatch, tmp_path, live):
    # a failed load may already have written some new content; reverting to the
    # recorded content must not be skipped as "unchanged"
    m = ["--manifest-dir", str(tmp_path / "m")]
    docs = [_doc("Dagger", weight=1), _doc("Melina 1", "npc_dialogue")]
    _load(monkeypatch, tmp_path, live, docs, *m)
    with pytest.raises(SystemExit):
        _load(monkeypatch, tmp_path, live, [_doc("Dagger", weight=9)], *m, errors=[{}])
    assert _load(monkeypatch, tmp_path, live, docs, *m) == ["Dagger"]
    saved = json.loads((tmp_path / "m/er-v2/1.05.json").read_text())
    assert sorted(saved) == ["npc_dialogue", "weapon"]


def test_partial_type_load_keeps_other_types(monkeypatch, tmp_path, live):
    # a dialogue-only reload replaces just npc_dialogue in the version's manifest
    m = ["--manifest-dir", str(tmp_path / "m")]
    items = [_doc("Dagger"), _doc("Melina 1", "npc_dialogue", description="a")]
    _load(monkeypatch, tmp_path, live, items, *m)
    dlg = [_doc("Melina 1", "npc_dialogue", description="b")]
    assert _load(monkeypatch, tmp_path, live, dlg, *m) == ["Melina 1"]
    saved = json.loads((tmp_path / "m/er-v2/1.05.json").read_text())
    assert sorted(saved) == ["npc_dialogue", "weapon"]
    assert _load(monkeypatch, tmp_path, live, items, *m) == ["Melina 1"]


def test_without_manifest_dir_nothing_changes(monkeypatch, tmp_path, live):
    docs = [_doc("Dagger")]
    assert _load(monkeypatch, tmp_path, live, docs) == ["Dagger"]
    assert _load(monkeypatch, tmp_path, live, docs) == ["Dagger"]
    assert live.scans == 0 and not list(tmp_path.glob("**/*.lock"))
