"""Loader robustness (#448): offline mapping check, bulk retries, ensure_index race
and alias guard, the refresh-independent prune guard, corrupt manifests and
index_settings. No OpenSearch needed."""

import json
import sys
from pathlib import Path

import pytest
from opensearchpy import RequestError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import index_settings  # noqa: E402
import load_data  # noqa: E402
import load_dataset  # noqa: E402
import load_manifest  # noqa: E402
from elden_ring import _client  # noqa: E402

_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "name": {"type": "keyword"},
            "scaling": {
                "properties": {
                    "str": {"properties": {"grade": {"type": "keyword"}}},
                }
            },
            "drops": {"properties": {"item": {"type": "keyword"}}},
            "placements": {"type": "object", "enabled": False},
            "ar_inputs": {"type": "object", "enabled": False},
        },
    }
}


# -- offline strict-mapping check ---------------------------------------------


def test_unmapped_paths_walks_objects_and_lists(monkeypatch):
    monkeypatch.setattr(_client, "INDEX_MAPPING", _MAPPING)
    docs = [
        {
            "name": "Dagger",
            "scaling": {"str": {"grade": "D", "value": 9}, "dex": None},
            "drops": [{"item": "Rune"}, {"item": "Arc", "chance": 5}],
            "placements": [{"anything": {"goes": 1}}],
            "ar_inputs": {"x": 1},
            "unknown": None,
        },
        {"name": "Club", "new_leaf": 1, "scaling": {"int": {"grade": "E"}}},
    ]
    assert _client.unmapped_paths(docs) == [
        "drops.chance",
        "new_leaf",
        "scaling.int",
        "scaling.str.value",
    ]


def test_unmapped_paths_skips_internal_fields(monkeypatch):
    mapping = {"mappings": {"properties": {"name": {"type": "keyword"}}}}
    monkeypatch.setattr(_client, "INDEX_MAPPING", mapping)
    assert _client.unmapped_paths([{"name": "A", "ar_inputs": {"a": 1}}]) == []


def test_real_mapping_accepts_its_own_shape():
    assert _client.unmapped_paths([{"name": "A", "entity_type": "weapon"}]) == []
    assert _client.unmapped_paths([{"name": "A", "zz_not_a_field": 1}]) == [
        "zz_not_a_field"
    ]


@pytest.mark.parametrize("flags", [(), ("--dry-run",)])
def test_load_dataset_refuses_unmapped_before_connecting(monkeypatch, tmp_path, flags):
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps([{"entity_type": "weapon", "name": "A", "zz_bad": 1}]))

    def _no_client():
        raise AssertionError("connected before the mapping check")

    monkeypatch.setattr(load_dataset, "_get_client", _no_client)
    monkeypatch.setattr(sys, "argv", ["load_dataset.py", str(ds), *flags])
    with pytest.raises(SystemExit) as e:
        load_dataset.main()
    assert "zz_bad" in str(e.value.code)


# -- bulk retries ---------------------------------------------------------------


def test_client_retries_timeouts(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "localhost")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "x")
    t = load_data._get_client().transport
    assert t.retry_on_timeout and t.max_retries == 3


def test_load_documents_bulk_retries_429(monkeypatch):
    calls = []

    def _bulk(client, actions, **kw):
        calls.append((list(actions), kw))
        return len(calls[-1][0]), []

    monkeypatch.setattr(load_data, "bulk", _bulk)
    docs = [{"entity_type": "weapon", "name": "A", "patch_version": "1.02", "x": None}]
    assert load_data.load_documents(object(), docs, dry_run=False) == (1, [])
    ((actions, kw),) = calls
    assert actions == [
        {
            "_index": load_data.INDEX,
            "_id": "weapon::A::1.02",
            "_source": {"entity_type": "weapon", "name": "A", "patch_version": "1.02"},
        }
    ]
    assert kw["max_retries"] == 3 and kw["initial_backoff"] == 2
    assert not kw["raise_on_error"]
    assert load_data.load_documents(object(), docs, dry_run=True) == (0, [])
    assert load_data.load_documents(object(), [{"entity_type": "x"}], False) == (0, [])
    assert len(calls) == 1


# -- ensure_index -----------------------------------------------------------------


class _Indices:
    def __init__(self, exists, create_error=None):
        self._exists, self.create_error, self.calls = exists, create_error, []

    def exists(self, index):
        return self._exists

    def create(self, index, body):
        self.calls.append(("create", index))
        if self.create_error:
            raise self.create_error

    def put_settings(self, index, body):
        self.calls.append(("put_settings", index))

    def put_mapping(self, index, body):
        self.calls.append(("put_mapping", index))


class _Client:
    def __init__(self, **kw):
        self.indices = _Indices(**kw)


def test_ensure_index_creates_missing_concrete_index(monkeypatch):
    monkeypatch.setattr(load_data, "INDEX", "elden-ring-entities-v9")
    c = _Client(exists=False)
    load_data.ensure_index(c)
    assert c.indices.calls == [("create", "elden-ring-entities-v9")]


def test_ensure_index_updates_existing(monkeypatch):
    c = _Client(exists=True)
    load_data.ensure_index(c)
    assert [k for k, _ in c.indices.calls] == ["put_settings", "put_mapping"]


def test_ensure_index_lost_create_race_puts_mapping(monkeypatch):
    monkeypatch.setattr(load_data, "INDEX", "elden-ring-entities-v9")
    err = RequestError(400, "resource_already_exists_exception", {})
    c = _Client(exists=False, create_error=err)
    load_data.ensure_index(c)
    assert [k for k, _ in c.indices.calls] == ["create", "put_settings", "put_mapping"]


def test_ensure_index_reraises_other_create_errors(monkeypatch):
    monkeypatch.setattr(load_data, "INDEX", "elden-ring-entities-v9")
    c = _Client(exists=False, create_error=RequestError(400, "mapper_parsing", {}))
    with pytest.raises(RequestError):
        load_data.ensure_index(c)


def test_ensure_index_refuses_to_create_under_alias_name(monkeypatch, tmp_path):
    monkeypatch.setattr(load_data, "INDEX", "elden-ring-entities")
    c = _Client(exists=False)
    with pytest.raises(load_data.IndexMissing):
        load_data.ensure_index(c)
    assert c.indices.calls == []
    # load_dataset turns it into a non-zero exit for the reload scripts
    monkeypatch.setattr(load_dataset, "ensure_index", load_data.ensure_index)
    monkeypatch.setattr(load_dataset, "_get_client", lambda: c)
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps([{"entity_type": "weapon", "name": "A"}]))
    monkeypatch.setattr(sys, "argv", ["load_dataset.py", str(ds)])
    with pytest.raises(SystemExit) as e:
        load_dataset.main()
    assert "refusing to create" in str(e.value.code)


# -- prune guard -------------------------------------------------------------------


def _docs(names, t="enemy", v="1.02"):
    return [{"entity_type": t, "name": n, "patch_version": v} for n in names]


def _prune(monkeypatch, live_names, built_names, **kw):
    """Run the real prune_stale (real _stale_ids) over fake scan + bulk."""
    deleted = []
    live = [{"_id": f"enemy::{n}::1.02"} for n in live_names]
    monkeypatch.setattr(load_data, "scan", lambda client, **k: iter(live))

    def _bulk(client, actions, **k):
        deleted.extend(a["_id"] for a in actions)
        return len(deleted), []

    monkeypatch.setattr(load_data, "bulk", _bulk)
    result = load_data.prune_stale(object(), _docs(built_names), False, **kw)
    return result, deleted


@pytest.mark.parametrize("refreshed", [False, True])
def test_rename_of_most_docs_is_allowed_whatever_the_refresh_state(
    monkeypatch, refreshed
):
    old = [f"Old{i}" for i in range(10)]
    built = [f"Old{i}" for i in range(4)] + [f"New{i}" for i in range(6)]
    # with refresh on, the docs just written are already visible to the scan
    live = old + (built[4:] if refreshed else [])
    (n, errors), deleted = _prune(monkeypatch, live, built)
    assert n == 6 and errors == []
    assert sorted(deleted) == sorted(f"enemy::Old{i}::1.02" for i in range(4, 10))


def test_truncated_build_is_refused(monkeypatch):
    old = [f"K{i}" for i in range(10)]
    built = old[:3]
    with pytest.raises(load_data.PruneRefused, match="7 of 10 enemy"):
        _prune(monkeypatch, old, built)
    # nothing deleted, and an override lets it through
    (n, _), deleted = _prune(monkeypatch, old, built, max_fraction=1.0)
    assert n == 7 and len(deleted) == 7
    (n, _), _ = _prune(monkeypatch, old, built, max_fraction=0.8)
    assert n == 7


def _run_prune_main(monkeypatch, tmp_path, *flags, refuse=True):
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps(_docs(["A"])))
    seen = []
    monkeypatch.setattr(load_dataset, "_get_client", lambda: object())
    monkeypatch.setattr(load_dataset, "ensure_index", lambda c, recreate: None)
    monkeypatch.setattr(load_dataset, "load_documents", lambda c, d, dry_run: (1, []))

    def _prune_stale(c, docs, dry_run, max_fraction):
        seen.append(max_fraction)
        if refuse and max_fraction < 1:
            raise load_data.PruneRefused("refusing to prune: 9 of 10")
        return 9, []

    monkeypatch.setattr(load_dataset, "prune_stale", _prune_stale)
    monkeypatch.setattr(sys, "argv", ["load_dataset.py", str(ds), *flags])
    load_dataset.main()
    return seen


def test_prune_refusal_exits_nonzero(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        _run_prune_main(monkeypatch, tmp_path, "--prune")
    assert e.value.code not in (0, None) and "refusing to prune" in e.value.code


def test_prune_overrides(monkeypatch, tmp_path):
    assert _run_prune_main(monkeypatch, tmp_path, "--force-prune", "--prune") == [1.0]
    seen = _run_prune_main(
        monkeypatch, tmp_path, "--prune-max-fraction", "0.9", "--prune", refuse=False
    )
    assert seen == [0.9]
    with pytest.raises(SystemExit):  # the two overrides are exclusive
        _run_prune_main(
            monkeypatch, tmp_path, "--force-prune", "--prune-max-fraction", "1"
        )


# -- corrupt manifest ------------------------------------------------------------


def test_corrupt_manifest_reads_empty_and_is_rewritten(tmp_path, capsys):
    path = tmp_path / "idx" / "1.02.json"
    path.parent.mkdir()
    path.write_text('{"enemy": {"enemy::A::1.02": "ab')  # torn write
    assert load_manifest._read(path) == {}
    assert "corrupt load manifest" in capsys.readouterr().out
    load_manifest.record(_docs(["A"]), tmp_path, "idx")
    assert list(json.loads(path.read_text())["enemy"]) == ["enemy::A::1.02"]


# -- index_settings --------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "restore"])
def test_index_settings_main(monkeypatch, action):
    calls = []
    client = object()
    monkeypatch.setattr(index_settings, "_get_client", lambda: client)
    for name in ("pause_refresh", "restore_refresh"):
        monkeypatch.setattr(
            index_settings, name, lambda c, name=name: calls.append((name, c))
        )
    monkeypatch.setattr(sys, "argv", ["index_settings.py", action])
    index_settings.main()
    assert calls == [(f"{action}_refresh", client)]
