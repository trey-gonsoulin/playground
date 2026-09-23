"""Unit tests for stale-doc pruning on reload (#67). No OpenSearch needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from load_data import _stale_ids  # noqa: E402


def _doc(entity_type, name, version="1.02"):
    return {"entity_type": entity_type, "name": name, "patch_version": version}


def test_stale_ids_returns_live_ids_missing_from_docs():
    docs = [_doc("enemy", "Margit, the Fell Omen")]
    live = [
        "enemy::Margit, the Fell Omen::1.02",
        "enemy::Messmer the Impaler::1.02",
    ]
    assert _stale_ids(live, docs) == ["enemy::Messmer the Impaler::1.02"]


def test_stale_ids_empty_when_every_live_id_is_rebuilt():
    docs = [_doc("enemy", "Margit, the Fell Omen"), _doc("weapon", "Dagger")]
    live = ["enemy::Margit, the Fell Omen::1.02", "weapon::Dagger::1.02"]
    assert _stale_ids(live, docs) == []


def test_stale_ids_empty_live_set():
    assert _stale_ids([], [_doc("enemy", "Margit, the Fell Omen")]) == []
