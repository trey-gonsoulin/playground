"""Loader exit status (#411, #415) and reindex load settings (#406). No OpenSearch
needed."""

import json
import sys

import pytest

import load_data
import load_dataset


def _run_main(monkeypatch, tmp_path, errors, *flags, prune_errors=()):
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps([{"entity_type": "weapon", "name": "Dagger"}]))
    pruned = []
    monkeypatch.setattr(load_dataset, "_get_client", object)
    monkeypatch.setattr(load_dataset, "ensure_index", lambda c, recreate: None)
    monkeypatch.setattr(
        load_dataset, "load_documents", lambda c, docs, dry_run: (1, errors)
    )

    def _prune(c, docs, dry_run, max_fraction):
        pruned.append(max_fraction)
        return 0, list(prune_errors)

    monkeypatch.setattr(load_dataset, "prune_stale", _prune)
    monkeypatch.setattr(sys, "argv", ["load_dataset.py", str(ds), *flags])
    load_dataset.main()
    return pruned


def test_bulk_errors_exit_nonzero_without_prune(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, tmp_path, [{"index": {"status": 400}}])
    assert e.value.code not in (0, None)


def test_bulk_errors_with_prune_exit_nonzero_and_skip_prune(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, tmp_path, [{"index": {"status": 400}}], "--prune")
    assert "skipping prune" in str(e.value.code)


def test_prune_delete_errors_exit_nonzero(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        _run_main(
            monkeypatch,
            tmp_path,
            [],
            "--prune",
            prune_errors=[{"delete": {"status": 503}}],
        )
    assert "prune had 1 delete errors" in str(e.value.code)


def test_prune_stale_returns_delete_errors(monkeypatch):
    doc = {"entity_type": "enemy", "name": "A", "patch_version": "1.02"}
    live = [{"_id": "enemy::A::1.02"}, {"_id": "enemy::B::1.02"}]
    live += [{"_id": f"enemy::K{i}::1.02"} for i in range(8)]
    docs = [doc] + [
        {"entity_type": "enemy", "name": f"K{i}", "patch_version": "1.02"}
        for i in range(8)
    ]
    monkeypatch.setattr(load_data, "_stale_ids", lambda live_ids, d: ["enemy::B::1.02"])
    monkeypatch.setattr(load_data, "scan", lambda client, **kw: iter(live))
    err = {"delete": {"_id": "enemy::B::1.02", "status": 503}}
    monkeypatch.setattr(load_data, "bulk", lambda client, actions, **kw: (0, [err]))
    assert load_data.prune_stale(object(), docs, dry_run=False) == (0, [err])
    monkeypatch.setattr(load_data, "bulk", lambda client, actions, **kw: (1, []))
    assert load_data.prune_stale(object(), docs, dry_run=False) == (1, [])
    assert load_data.prune_stale(object(), docs, dry_run=True) == (0, [])


def test_clean_load_exits_zero(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, [], "--prune") == [0.5]
    assert _run_main(monkeypatch, tmp_path, []) == []


class _Indices:
    def __init__(self, names):
        self.names, self.calls = names, []

    def exists(self, index):
        return bool(self.names)

    def get_settings(self, index):
        # an alias resolves to its concrete index, as OpenSearch does
        return {n: {} for n in self.names}

    def put_settings(self, index, body):
        self.calls.append(("put", index, body))

    def refresh(self, index):
        self.calls.append(("refresh", index))


class _Client:
    def __init__(self, names=("elden-ring-entities-v3",)):
        self.indices = _Indices(list(names))


def test_pause_targets_concrete_index(monkeypatch):
    for k in ("ER_LOAD_PAUSE_REFRESH", "ER_LOAD_ASYNC_TRANSLOG"):
        monkeypatch.delenv(k, raising=False)
    c = _Client()
    assert load_data.pause_refresh(c) == ["elden-ring-entities-v3"]
    assert c.indices.calls == [
        (
            "put",
            "elden-ring-entities-v3",
            {"index.refresh_interval": "-1", "index.translog.durability": "async"},
        )
    ]


def test_pause_knobs(monkeypatch):
    monkeypatch.setenv("ER_LOAD_ASYNC_TRANSLOG", "0")
    monkeypatch.delenv("ER_LOAD_PAUSE_REFRESH", raising=False)
    c = _Client()
    load_data.pause_refresh(c)
    assert c.indices.calls[0][2] == {"index.refresh_interval": "-1"}
    monkeypatch.setenv("ER_LOAD_PAUSE_REFRESH", "off")
    c = _Client()
    assert load_data.pause_refresh(c) == [] and c.indices.calls == []


def test_restore_resets_defaults_and_refreshes(monkeypatch):
    # restore ignores the knobs: it always undoes everything pause can set
    monkeypatch.setenv("ER_LOAD_PAUSE_REFRESH", "0")
    c = _Client()
    load_data.restore_refresh(c)
    assert c.indices.calls == [
        (
            "put",
            "elden-ring-entities-v3",
            {"index.refresh_interval": None, "index.translog.durability": None},
        ),
        ("refresh", "elden-ring-entities-v3"),
    ]


def test_missing_index_is_a_noop():
    c = _Client(names=())
    assert load_data.pause_refresh(c) == [] == load_data.restore_refresh(c)
    assert c.indices.calls == []


def test_client_compresses_unless_turned_off(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "localhost")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "x")
    monkeypatch.delenv("ER_LOAD_COMPRESS", raising=False)
    conn = load_data._get_client().transport.get_connection()
    assert conn.http_compress
    monkeypatch.setenv("ER_LOAD_COMPRESS", "0")
    assert not load_data._get_client().transport.get_connection().http_compress
