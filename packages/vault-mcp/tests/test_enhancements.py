"""Versions, locks, git, byte-exact files, types, regex patches, templates,
validate, unresolved links, Bases and scripts."""

import base64
import hashlib
import json
import subprocess

import pytest
from conftest import audit
from mcp.server.mcpserver.exceptions import ToolError
from vault_mcp import _notes
from vault_mcp import mcp_server as t
from vault_mcp.config import get_settings

# Called directly (not through the server), vault and lock errors are ValueErrors.
REFUSED = (ToolError, ValueError)

# --- versions and receipts --------------------------------------------------------


def test_read_note_version_and_if_version(vault):
    note = t.read_note("Rug Options")
    raw = (vault / "Home/Rug Options.md").read_bytes()
    assert note["version"] == hashlib.sha256(raw).hexdigest()
    assert note["bytes"] == len(raw)
    out = t.update_note(
        "Rug Options",
        patch=[{"find": "- wool", "replace": "- silk"}],
        if_version=note["version"],
    )
    new = (vault / "Home/Rug Options.md").read_bytes()
    assert out["version"] == hashlib.sha256(new).hexdigest()
    assert out["bytes"] == len(new)
    # The old version is stale now: refused, with the current text.
    with pytest.raises(ToolError, match="changed since version") as err:
        t.append_to_note("Rug Options", "- more", if_version=note["version"])
    assert "- silk" in str(err.value)
    assert (vault / "Home/Rug Options.md").read_bytes() == new


def test_create_notes_and_update_notes_return_receipts(vault):
    out = t.create_notes([{"title": "A"}, {"title": "B"}])
    assert [r["path"] for r in out["receipts"]] == ["_Inbox/A.md", "_Inbox/B.md"]
    a = (vault / "_Inbox/A.md").read_bytes()
    assert out["receipts"][0]["version"] == hashlib.sha256(a).hexdigest()
    stale = out["receipts"][0]["version"]
    t.append_to_note("_Inbox/A.md", "x")
    plan = t.update_notes([{"note": "_Inbox/A.md", "body": "y", "if_version": stale}])
    assert "changed since" in plan["errors"][0]["error"]


# --- reading by heading and typed frontmatter -------------------------------------


def test_read_note_by_heading(vault):
    (vault / "Home/Long.md").write_text(
        "---\ntype: note\n---\n# Long\n\n## One\na\n### Sub\nb\n## Two\nc\n"
    )
    out = t.read_note("Long", heading="One")
    assert out["content"] == "## One\na\n### Sub\nb"
    assert "in section 'One'" in out["lines"]
    with pytest.raises(ToolError, match="Three"):
        t.read_note("Long", heading="Three")


def test_typed_frontmatter_and_quoting(vault):
    (vault / "Home/Typed.md").write_text(
        '---\nentries: 13\nmenu-index: "10.10"\ndlc: false\nlooks: 10.10\n'
        "created: 2026-11-12\n---\n# Typed\n"
    )
    fm = t.read_note("Typed")["frontmatter"]
    assert fm == {
        "entries": 13,
        "menu-index": "10.10",
        "dlc": False,
        "looks": 10.1,
        "created": "2026-11-12",
    }
    t.update_note("Typed", properties={"menu-index": "10.1", "batch": 7, "dlc": True})
    text = (vault / "Home/Typed.md").read_text()
    assert 'menu-index: "10.1"' in text and "batch: 7" in text and "dlc: true" in text


def test_property_type_conflicts_are_refused(vault):
    for i in range(3):
        (vault / f"Home/W{i}.md").write_text(
            f"---\nentries: {i}\ncharacters:\n  - x\n---\n# W{i}\n"
        )
    with pytest.raises(ToolError, match="pass it as a number"):
        t.update_note("W0", properties={"entries": "13"})
    with pytest.raises(ToolError, match="pass a list"):
        t.create_note("W9", folder="Home", properties={"characters": "81"})
    t.update_note("W0", properties={"entries": 13})  # the right type is fine


# --- regex patches ----------------------------------------------------------------


def test_regex_patch_with_count(vault):
    t.update_note(
        "Rug Options",
        patch=[{"find_regex": r"^- (\w+)$", "replace": r"- [\1]", "count": 1}],
    )
    assert "- [wool]" in (vault / "Home/Rug Options.md").read_text()
    with pytest.raises(ToolError, match="not the expected 5"):
        t.update_note(
            "Rug Options", patch=[{"find_regex": r"^- ", "replace": "* ", "count": 5}]
        )
    with pytest.raises(ToolError, match="bad find_regex"):
        t.update_note("Rug Options", patch=[{"find_regex": "(", "replace": ""}])


# --- templates --------------------------------------------------------------------


def test_create_note_from_template(vault):
    (vault / "_Templates/Weapon.md").write_text(
        "---\ntype: reference\nstatus: active\ncreated: {{date}}\ncatalogue: weapon\n"
        'entries:\nmenu-index: ""\n---\n# {{title}}\n\n**Provenance:** \n'
    )
    out = t.create_note(
        "Dagger",
        folder="Lore",
        template="Weapon",
        properties={"entries": 13, "menu-index": "10.10"},
        patch=[{"find": "**Provenance:** ", "replace": "**Provenance:** a shop"}],
    )
    text = (vault / "Lore/Dagger.md").read_text()
    assert "# Dagger" in text and "a shop" in text
    assert "catalogue: weapon" in text and "entries: 13" in text
    assert 'menu-index: "10.10"' in text and "{{date}}" not in text
    assert out["frontmatter"]["entries"] == 13
    with pytest.raises(ToolError, match="no template"):
        t.create_note("X", template="Nope")


# --- bulk_update ------------------------------------------------------------------


def test_bulk_update_where_query_and_patch(vault):
    args = {
        "patch": [{"find": "[[Rug Options", "replace": "[[Rugs", "replace_all": True}],
        "where": {"type": "list"},
        "query": "lamp",
    }
    plan = t.bulk_update(**args)
    assert [p["path"] for p in plan["plan"]] == ["Home/Shopping.md"]
    assert "+Need [[Rugs|a rug]]" in plan["plan"][0]["diff"]
    out = t.bulk_update(**args, dry_run=False)
    assert out["receipts"][0]["path"] == "Home/Shopping.md"
    assert "[[Rugs#Wool]]" in (vault / "Home/Shopping.md").read_text()


def test_bulk_update_is_all_or_nothing(vault):
    before = (vault / "Home/Rug Options.md").read_text()
    out = t.bulk_update(
        patch=[{"find": "- wool", "replace": "- silk"}], folder="Home", dry_run=False
    )
    assert out["dry_run"] is True and out["errors"]
    assert (vault / "Home/Rug Options.md").read_text() == before


# --- byte-exact files -------------------------------------------------------------


def test_write_file_and_read_file(vault, tmp_path):
    text = "---\nweird:   spacing\n---\r\nno heading, CRLF\r\n日本語\n"
    with pytest.raises(ToolError, match="say how"):
        t.write_file("Lore/Full.md", text)
    out = t.write_file("Lore/Full.md", text, if_absent=True)
    raw = (vault / "Lore/Full.md").read_bytes()
    assert raw == text.encode() and out["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(REFUSED, match="already exists"):
        t.write_file("Lore/Full.md", "x", if_absent=True)
    back = t.read_file("Lore/Full.md")
    assert back["content"] == text and back["sha256"] == out["sha256"]
    # A chunk boundary inside a multi-byte character backs off to the character.
    part = t.read_file("Lore/Full.md", offset=0, length=len(raw) - 3)
    assert part["encoding"] == "utf-8" and part["next_offset"] < len(raw)
    # Replace in staged chunks, checked against the whole file's sha256.
    new = "A" * 10 + "B" * 10
    t.write_file("Lore/Full.md", new[:10], more=True)
    with pytest.raises(ToolError, match="sha256"):
        t.write_file(
            "Lore/Full.md", new[10:], if_version=out["sha256"], sha256="0" * 64
        )
    done = t.write_file(
        "Lore/Full.md",
        new[10:],
        if_version=out["sha256"],
        sha256=hashlib.sha256(new.encode()).hexdigest(),
    )
    assert (vault / "Lore/Full.md").read_text() == new
    with pytest.raises(REFUSED, match="changed since"):
        t.write_file("Lore/Full.md", "x", if_version=out["sha256"])
    png = base64.b64encode(b"\x89PNG\x00").decode()
    t.write_file("Lore/a.png", png, encoding="base64", if_absent=True)
    assert t.read_file("Lore/a.png")["encoding"] == "base64"
    assert audit(tmp_path)[-1]["tool"] == "write_file"
    assert done["bytes"] == 20


# --- validate and unresolved links ------------------------------------------------


def test_validate(vault):
    (vault / "Lore").mkdir()
    (vault / "Lore/Q1.md").write_text("---\nquestion-status: open\nentries: 3\n---\n")
    (vault / "Lore/Q2.md").write_text(
        '---\nquestion-status: maybe\nentries: "3"\n---\n'
    )
    (vault / "Lore/Q3.md").write_text("---\nentries: 3\n---\n")
    out = t.validate(
        {
            "required": ["question-status"],
            "allowed": {"question-status": ["open", "settled", "declined"]},
            "types": {"entries": "number"},
        },
        folder="Lore",
    )
    assert out["checked"] == 3 and out["ok"] == 1
    problems = {n["path"]: n["problems"] for n in out["notes"]}
    assert problems["Lore/Q3.md"] == ["missing question-status"]
    assert len(problems["Lore/Q2.md"]) == 2


def test_unresolved_links_and_table_escapes(vault):
    (vault / "Home/map.png").write_bytes(b"png")
    (vault / "Home/Table.md").write_text(
        "| a | [[Rug Options\\|rugs]] |\n![[map.png]] [[Missing]] [[Missing]]\n"
    )
    out = t.unresolved_links()
    targets = {u["target"]: u for u in out["unresolved"]}
    assert "Missing" in targets and targets["Missing"]["notes"] == ["Home/Table.md"]
    assert not any(k.endswith("\\") for k in targets)
    assert "map.png" not in targets
    note = t.read_note("Table")
    assert "Home/Rug Options.md" in note["links"] and "Home/map.png" in note["links"]
    assert note["unresolved_links"] == ["Missing"]


# --- locks and git ----------------------------------------------------------------


@pytest.fixture
def repo(vault, tmp_path, monkeypatch):
    git_dir = tmp_path / "vault.git"
    env = {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(vault)}
    subprocess.run(["git", "init", "-q"], env=env, check=True)  # noqa: S607
    monkeypatch.setenv("VAULT_GIT_DIR", str(git_dir))
    get_settings.cache_clear()
    t._vault.cache_clear()
    t.git_commit("initial")
    return git_dir


def test_git_tools(repo, vault):
    assert t.git_status()["total"] == 0
    t.update_note("Rug Options", patch=[{"find": "- wool", "replace": "- silk"}])
    (vault / "Home/New.md").write_text("new\n")
    st = {i["path"]: i["status"] for i in t.git_status()["changed"]}
    assert st == {"Home/Rug Options.md": "M", "Home/New.md": "??"}
    diff = t.git_diff("Rug Options")
    assert "+- silk" in diff["diff"] and "Rug Options" in diff["stat"]
    assert t.git_diff()["new_files_not_in_diff"] == ["Home/New.md"]
    out = t.git_commit("swap rugs", paths=["Rug Options"])
    assert out["total_files"] == 1
    assert [i["path"] for i in t.git_status()["changed"]] == ["Home/New.md"]
    log = t.git_log("Home/Rug Options.md")["commits"]
    assert [c["message"] for c in log] == ["swap rugs", "initial"]
    assert log[0]["files"] == ["Home/Rug Options.md"]
    old = t.git_show("Home/Rug Options.md", rev="HEAD~1")
    assert "- wool" in old["content"]
    with pytest.raises(ToolError, match="not a revision"):
        t.git_diff(rev="--output=/tmp/x")


def test_write_locks(repo, vault, tmp_path):
    lk = t.begin_write("session A", "batch 21", folders=["Home"], minutes=5)
    lock = lk["lock"]
    with pytest.raises(REFUSED, match="reserved by"):
        t.append_to_note("Rug Options", "- x")
    with pytest.raises(REFUSED, match="reserved by"):
        t.move_note("Shopping", "Shop")
    with pytest.raises(ToolError, match="overlaps"):
        t.begin_write("session B", "other", paths=["Home/Shopping.md"])
    t.append_to_note("Rug Options", "- x", lock=lock)
    t.create_note("Locked", folder="Home", lock=lock)
    with pytest.raises(REFUSED, match="outside lock"):
        t.create_note("Elsewhere", lock=lock)
    t.create_note("Elsewhere")  # unlocked paths stay free for everyone
    assert t.git_status()["locks"][0]["owner"] == "session A"
    with pytest.raises(ToolError, match="break_lock"):
        t.end_write(lock, "session B", "mine now")
    done = t.end_write(lock, "session A", "Batch 21")
    assert done["commit"]["total_files"] == 2  # only the locked folder
    assert t.git_log(n=1)["commits"][0]["message"].startswith("Batch 21")
    assert "_Inbox/Elsewhere.md" in {i["path"] for i in t.git_status()["changed"]}
    t.append_to_note("Rug Options", "- y")  # released
    with pytest.raises(REFUSED, match="expired or was released"):
        t.append_to_note("Rug Options", "- z", lock=lock)
    lk2 = t.begin_write("gone", "crashed", paths=["Shopping"])
    out = t.end_write(lk2["lock"], "me", "x", break_lock=True)
    assert out["broken"] is True
    assert audit(tmp_path)[-1]["tool"] == "break_lock"


def test_end_write_skips_ignored_and_missing_paths(repo, vault):
    (vault / ".gitignore").write_text("/_Meta/\n")
    (vault / "_Meta").mkdir(exist_ok=True)
    t.git_commit("ignore _Meta")
    lk = t.begin_write(
        "s", "batch", paths=["Rug Options", "_Meta/Enhancements.md", "Home/Never.md"]
    )
    t.append_to_note("Rug Options", "- x", lock=lk["lock"])
    t.write_file("_Meta/Enhancements.md", "- idea\n", if_absent=True, lock=lk["lock"])
    commit = t.end_write(lk["lock"], "s", "batch")["commit"]
    assert [f["path"] for f in commit["files"]] == ["Home/Rug Options.md"]
    assert commit["ignored"] == ["_Meta/Enhancements.md"]
    lk = t.begin_write("s", "only ignored", paths=["_Meta/Enhancements.md"])
    commit = t.end_write(lk["lock"], "s", "x")["commit"]
    assert commit["commit"] is None and commit["ignored"] == ["_Meta/Enhancements.md"]


def test_snapshot_leaves_locked_paths_for_end_write(repo, vault, monkeypatch):
    monkeypatch.setenv("VAULT_SNAPSHOT_CMD", "false")  # must not run while locked
    get_settings.cache_clear()
    t._vault.cache_clear()
    lk = t.begin_write("s", "batch", folders=["Home"])
    t.append_to_note("Rug Options", "- x", lock=lk["lock"])
    (vault / "Other.md").write_text("other\n")
    out = t.bulk_update(
        folder="Home", set_properties={"batch": "21"}, dry_run=False, lock=lk["lock"]
    )
    assert out["snapshot"].startswith("ok")
    snap = t.git_log(n=1)["commits"][0]
    assert snap["message"].startswith("before bulk_update")
    assert snap["files"] == ["Other.md"]
    done = t.end_write(lk["lock"], "s", "Batch")["commit"]
    assert "Home/Rug Options.md" in {f["path"] for f in done["files"]}
    whole = t.begin_write("s", "all", folders=["/"])
    assert t._snapshot(get_settings(), "x") == "skipped: the whole vault is locked"
    t.end_write(whole["lock"], "s", "done")


# --- Bases ------------------------------------------------------------------------


BASE = """views:
  - type: table
    name: By class
    groupBy:
      property: weapon-class
    order: [file.name, weapon-class, entries]
    summaries:
      entries: Sum
properties:
  note.weapon-class:
    displayName: Class
"""


def test_query_base(fake_cli, vault, monkeypatch):
    (vault / "Lore").mkdir()
    (vault / "Lore/Weapons.base").write_text(BASE)
    (vault / "Lore/Catalogue.md").write_text(
        "# Catalogue\n![[Weapons.base#By class]]\n"
    )
    rows = [
        {"path": "a.md", "file.name": "a", "Class": "Dagger", "entries": 13},
        # base:query returns property values as text
        {"path": "b.md", "file.name": "b", "Class": "Dagger", "entries": "1"},
        {"path": "c.md", "file.name": "c", "Class": "Axe", "entries": "13"},
    ]
    monkeypatch.setenv("FAKE_BASE_ROWS", json.dumps(rows))
    out = t.query_base("Catalogue")
    assert out["base"] == "Lore/Weapons.base" and out["view"] == "By class"
    assert out["total"] == 3 and out["grouped_by"] == "Class"
    assert out["summaries"]["entries"]["value"] == 27
    assert out["summaries"]["entries"]["by_group"] == {"Dagger": 14, "Axe": 13}
    call = fake_cli()[-1]
    assert "base:query" in call and "path=Lore/Weapons.base" in call
    (vault / "Lore/Inline.md").write_text("# I\n```base\nviews: []\n```\n")
    with pytest.raises(ToolError, match="inline"):
        t.query_base("Inline")


def test_query_base_needs_the_app(vault):
    (vault / "Home/X.base").write_text("views: []\n")
    with pytest.raises(ToolError, match="couldn't query"):
        t.query_base("Home/X.base")


# --- scripts ----------------------------------------------------------------------

SCRIPT = """import pathlib, sys
root = pathlib.Path(sys.argv[1])
(root / "Home/Generated.md").write_text("# gen " + " ".join(sys.argv[2:]) + "\\n")
(pathlib.Path.home() / "summary.json").write_text('{"n": 1}')
print("done")
"""


def test_run_script_needs_a_pinned_hash(vault, monkeypatch, tmp_path):
    scripts = vault / "_Meta/scripts"
    scripts.mkdir(parents=True)
    (scripts / "gen.py").write_text(SCRIPT)
    digest = hashlib.sha256(SCRIPT.encode()).hexdigest()
    with pytest.raises(ToolError, match=f"gen.py={digest}"):
        t.run_script("gen.py")
    monkeypatch.setenv("VAULT_SCRIPTS", f"gen.py={digest}")
    get_settings.cache_clear()
    t._vault.cache_clear()
    out = t.run_script("gen.py", ["x"])
    assert out["exit_code"] == 0 and out["stdout"].strip() == "done"
    assert out["changed"] == ["Home/Generated.md"]
    assert out["outputs"] == {"summary.json": '{"n": 1}'}
    assert (vault / "Home/Generated.md").read_text() == "# gen x\n"
    out = t.run_script("gen.py", ["x"])  # rewrites the note with the same text
    assert out["changed"] == [] and out["changed_count"] == 0
    assert audit(tmp_path)[-1]["tool"] == "run_script"
    (scripts / "gen.py").write_text(SCRIPT + "# edited\n")
    with pytest.raises(ToolError, match="changed since it was approved"):
        t.run_script("gen.py")
    with pytest.raises(ToolError, match="file name"):
        t.run_script("../gen.py")


def test_tags_skip_links_and_code():
    body = (
        "See [[#Next batch]], [[Carried Questions#Open|open]], [x](#Later) "
        "and `#code`. Real: #lore #elden-ring/weapons\n"
    )
    assert _notes.tags({}, body) == ["elden-ring/weapons", "lore"]


def test_kind():
    assert [_notes.kind(x) for x in (1, 1.5, True, "a", "2026-01-01", [1], None)] == [
        "number",
        "number",
        "boolean",
        "text",
        "date",
        "list",
        None,
    ]
