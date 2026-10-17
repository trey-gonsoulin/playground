"""Opt-in gate and throwaway index for the live suite (#445), checked offline."""

import tomllib
import types
from pathlib import Path

import pytest
from _live_index import (
    LIVE_ALIAS,
    OPT_IN_ENV,
    STACK_ENV,
    check_throwaway,
    live_enabled,
    throwaway_index,
)


class _Indices:
    def __init__(self, indices=(), aliases=()):
        self.indices = set(indices)
        self.aliases = set(aliases)
        self.calls = []

    def exists(self, index):
        return index in self.indices or index in self.aliases

    def exists_alias(self, name):
        return name in self.aliases

    def create(self, index, body):
        self.calls.append(("create", index))
        self.indices.add(index)

    def delete(self, index, ignore=()):
        self.calls.append(("delete", index))
        self.indices.discard(index)


class _Client:
    def __init__(self, **kw):
        self.indices = _Indices(**kw)


def test_addopts_deselect_live_marker():
    cfg = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    ini = cfg["tool"]["pytest"]["ini_options"]
    assert "-m 'not live'" in ini["addopts"]
    assert any(m.startswith("live:") for m in ini["markers"])


def test_live_modules_carry_marker():
    import test_integration
    import test_user_dictionary

    names = [m.name for m in test_integration.pytestmark]
    assert names[0] == "live" and "skipif" in names
    marks = test_user_dictionary.test_tokens_on_throwaway_index.pytestmark
    assert "live" in [m.name for m in marks]


def test_live_needs_opt_in_and_stack_env(monkeypatch):
    for v in STACK_ENV:
        monkeypatch.setenv(v, "x")
    monkeypatch.delenv(OPT_IN_ENV, raising=False)
    assert not live_enabled()
    monkeypatch.setenv(OPT_IN_ENV, "1")
    assert live_enabled()
    monkeypatch.delenv(STACK_ENV[0])
    assert not live_enabled()


@pytest.mark.parametrize(
    "name", [LIVE_ALIAS, f"{LIVE_ALIAS}-v4", "elden-ring-kuromoji-test-53"]
)
def test_check_refuses_non_throwaway(name):
    with pytest.raises(RuntimeError):
        check_throwaway(_Client(), name)


def test_check_refuses_alias_or_existing():
    with pytest.raises(RuntimeError, match="alias"):
        check_throwaway(_Client(aliases={"elden-ring-it-a"}), "elden-ring-it-a")
    with pytest.raises(RuntimeError, match="exists"):
        check_throwaway(_Client(indices={"elden-ring-it-b"}), "elden-ring-it-b")
    check_throwaway(_Client(), "elden-ring-it-c")


def test_throwaway_index_patches_and_cleans_up():
    client = _Client(aliases={LIVE_ALIAS})
    mods = [types.SimpleNamespace(INDEX=LIVE_ALIAS) for _ in range(2)]
    with pytest.raises(ValueError):
        with throwaway_index(client, {"mappings": {}}, mods) as index:
            assert index.startswith("elden-ring-it-") and len(index) == 26
            assert all(m.INDEX == index for m in mods)
            assert client.indices.calls == [("create", index)]
            raise ValueError("test body failed")
    assert all(m.INDEX == LIVE_ALIAS for m in mods)
    assert client.indices.calls == [("create", index), ("delete", index)]
    assert client.indices.aliases == {LIVE_ALIAS}
