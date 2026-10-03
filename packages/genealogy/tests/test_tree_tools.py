import contextlib

import httpx
import pytest

from genealogy import mcp_server as srv
from genealogy._models import Person, Tree
from genealogy._storage import ConcurrentModification, LocalStore


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.delenv("TREE_BUCKET", raising=False)
    monkeypatch.setenv("TREE_DIR", str(tmp_path))
    return tmp_path


def test_build_tree_and_export(local_store):
    ann = srv.tree_add_person(
        given="Ann", surname="Walsh", sex="F", birth_date="ABT 1890"
    )["added"]
    john = srv.tree_add_person(given="John", surname="Walsh", sex="M")["added"]
    mary = srv.tree_add_person(given="Mary", surname="Burke", sex="F")["added"]
    srv.tree_link("parent", ann["id"], john["id"])
    srv.tree_link("parent", ann["id"], mary["id"])
    srv.tree_set_event(
        ann["id"], "birt", date="12 MAR 1890", citation_title="Parish register"
    )
    srv.tree_set_event(ann["id"], "RESI", place="Cork")
    srv.tree_set_event(ann["id"], "RESI", place="Boston")

    got = srv.tree_get_person(ann["id"])
    assert {p["name"] for p in got["parents"]} == {"John Walsh", "Mary Burke"}
    types = [e["type"] for e in got["person"]["events"]]
    assert types.count("BIRT") == 1 and types.count("RESI") == 2

    summary = srv.tree_summary()
    assert summary["people"] == 3 and summary["families"] == 1
    assert srv.tree_search_people("walsh ann")["count"] == 1

    out = srv.tree_export_gedcom(include_text=True)
    assert "0 @F1@ FAM" in out["gedcom"]
    paths = {f["path"] for f in srv.tree_list_files()["files"]}
    assert {"tree.json", "tree.ged"} <= paths
    assert any(p.startswith("exports/tree-") for p in paths)

    # GEDCOM import into a fresh tree name reproduces the shape.
    imported = srv.tree_import_gedcom(gedcom_text=out["gedcom"], tree="copy")
    assert imported == {"tree": "copy", "people": 3, "families": 1}
    assert srv.tree_list()["trees"] == ["copy", "default"]

    # Re-importing the stored export over a populated tree needs replace=True.
    with pytest.raises(ValueError, match="already has 3 people"):
        srv.tree_import_gedcom(stored_path="tree.ged")
    again = srv.tree_import_gedcom(stored_path="tree.ged", replace=True)
    assert again["people"] == 3


def test_remove_person_drops_empty_families():
    a = srv.tree_add_person(given="A")["added"]["id"]
    b = srv.tree_add_person(given="B")["added"]["id"]
    srv.tree_link("partner", a, b)
    srv.tree_remove_person(a)
    srv.tree_remove_person(b)
    assert srv.tree_summary()["families"] == 0


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        srv.tree_get_person("I999")
    with pytest.raises(ValueError):
        srv.tree_summary(tree="../etc")
    with pytest.raises(ValueError):
        srv.tree_file_url("../../secret")
    a = srv.tree_add_person(given="A", familysearch_id="X-1")["added"]["id"]
    with pytest.raises(ValueError, match="already in the tree"):
        srv.tree_add_person(given="A again", familysearch_id="X-1")
    with pytest.raises(ValueError):
        srv.tree_link("cousin", a, a)


def test_local_store_detects_concurrent_write(local_store):
    store = LocalStore(local_store)
    t1, v1 = store.load("t")
    t2, v2 = store.load("t")
    store.save(t1, v1)
    with pytest.raises(ConcurrentModification):
        store.save(t2, v2)


def test_model_rules_enforced_at_tool_layer():
    a = srv.tree_add_person(given="A", sex="female")["added"]
    assert a["sex"] == "F"
    assert srv.tree_update_person(a["id"], sex="x")["updated"]["sex"] == "U"
    with pytest.raises(ValueError, match="Unknown event type"):
        srv.tree_set_event(a["id"], "BIRTH", date="1900")
    b = srv.tree_add_person(given="B", familysearch_id="FS-1")["added"]["id"]
    with pytest.raises(ValueError, match="already in the tree"):
        srv.tree_update_person(a["id"], familysearch_id="FS-1")
    with pytest.raises(ValueError, match="No person"):
        srv.tree_link("parent", a["id"], "I999")
    srv.tree_update_person(b, familysearch_id="")
    assert srv.tree_get_person(b)["person"].get("external_ids") is None


def _tree_with(n: int) -> Tree:
    t = Tree()
    for _ in range(n):
        pid = t.new_person_id()
        t.people[pid] = Person(id=pid)
    return t


def test_completing_parents_keeps_half_siblings_and_no_duplicate_couple():
    t = _tree_with(4)  # I1, I2 = couple; I3, I4 = children of I1
    couple = t.family_for_partners("I1", "I2")
    t.add_child("I3", "I1", None)
    single = t.add_child("I4", "I1", None)
    assert single.id != couple.id and single.child_ids == ["I3", "I4"]

    t.add_child("I3", "I1", "I2")  # what import_ancestry does
    assert couple.child_ids == ["I3"]
    assert single.child_ids == ["I4"]  # half-sibling keeps only I1
    couples = [f for f in t.families.values() if set(f.partner_ids) == {"I1", "I2"}]
    assert couples == [couple]

    # A full-parent link is never downgraded by a later partial one.
    assert t.add_child("I3", "I1", None) is couple


def test_add_parent_with_siblings_moves_only_that_child():
    t = _tree_with(4)  # I1 parent of I3 and I4; I2 becomes I3's other parent
    single = t.add_parent("I3", "I1")
    t.add_parent("I4", "I1")
    fam = t.add_parent("I3", "I2")
    assert set(fam.partner_ids) == {"I1", "I2"} and fam.child_ids == ["I3"]
    assert single.partner_ids == ["I1"] and single.child_ids == ["I4"]

    # Sole child of a single-parent family: widened in place, nothing left behind.
    t2 = _tree_with(3)
    only = t2.add_parent("I3", "I1")
    assert t2.add_parent("I3", "I2") is only and len(t2.families) == 1


def test_attach_resources_never_collide_and_retry_is_noop(monkeypatch):
    bodies = {
        "https://a.example/x/image.jpg": b"first",
        "https://b.example/y/image.jpg": b"second",
    }
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=bodies[str(r.url)])
    )

    @contextlib.contextmanager
    def fake_stream(method, url, **kw):
        with httpx.Client(transport=transport) as c, c.stream(method, url) as resp:
            yield resp

    monkeypatch.setattr(srv.httpx, "stream", fake_stream)
    a = srv.tree_add_person(given="A")["added"]["id"]
    one = srv.tree_attach_resource_from_url("https://a.example/x/image.jpg", [a])
    two = srv.tree_attach_resource_from_url("https://b.example/y/image.jpg", [a])
    assert one["path"] != two["path"]
    assert srv.get_store().read_file("default", one["path"]) == b"first"
    srv.tree_attach_resource_from_url("https://a.example/x/image.jpg", [a])  # retry
    person = srv.tree_get_person(a)["person"]
    assert person["resources"] == [one["path"], two["path"]]
    assert len(person["citations"]) == 2
