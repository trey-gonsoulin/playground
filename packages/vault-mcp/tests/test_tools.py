"""Tool behaviour against a small vault shaped like the real one (conftest.FILES).

The scenarios mirror the brief's acceptance tests (§8), run once through the
filesystem backend and, where the CLI matters, through fake_obsidian_cli.py.
"""

from datetime import date

import pytest
from conftest import audit
from mcp.server.mcpserver.exceptions import ToolError
from vault_mcp import _notes
from vault_mcp import mcp_server as t

NPCS = "TTRPG/Murder on the Eberron Express/NPCs"


# --- §8.1 campaign questions ------------------------------------------------------


def test_list_and_read_campaign_npcs(vault):
    listed = t.list_notes(folder=NPCS)
    assert [n["path"] for n in listed["notes"]] == [f"{NPCS}/Vex.md"]
    note = t.read_note("Vex")
    assert "killer" in note["content"]
    assert note["frontmatter"]["up"] == "[[Murder on the Eberron Express]]"
    assert note["links"] == [
        "TTRPG/Murder on the Eberron Express/Murder on the Eberron Express.md"
    ]
    assert note["backlinks"] == [
        "TTRPG/Murder on the Eberron Express/Murder on the Eberron Express.md"
    ]


def test_list_notes_filters(vault):
    lists = t.list_notes(type="list", status="active")
    assert {n["title"] for n in lists["notes"]} == {
        "Rug Options",
        "Shopping",
        "Old list",
    }
    assert [n["title"] for n in t.list_notes(tag="eberron")["notes"]] == ["Vex"]
    assert [n["title"] for n in t.list_notes(person="Sammy")["notes"]] == [
        "Therapy topics"
    ]
    old = t.list_notes(type="list", updated_before="2023-01-01")
    assert {n["title"] for n in old["notes"]} == {"Rug Options", "Old list"}


def test_read_note_pages_long_bodies(vault):
    (vault / "Long.md").write_text("\n".join(f"line {i}" for i in range(50)))
    first = t.read_note("Long", limit=20)
    assert first["next_offset"] == 20 and first["lines"] == "1-20 of 50"
    last = t.read_note("Long", offset=40, limit=20)
    assert "next_offset" not in last and last["content"].endswith("line 49")


def test_find_by_alias_and_ambiguity(vault):
    assert t.read_note("Sam")["path"] == "People/Sam Rivera.md"
    (vault / "Personal" / "Vex.md").write_text("other vex")
    with pytest.raises(ToolError, match="several notes"):
        t.read_note("Vex")


# --- §8.2 / §8.6 search, CLI and fallback -------------------------------------------


def test_search_filesystem(vault):
    res = t.search("therapy session")
    assert res["backend"] == "filesystem"
    assert res["results"][0]["path"] == "Personal/Relationships/Therapy topics.md"
    assert "therapy session" in res["results"][0]["matches"][0]["text"].lower()


def test_search_uses_cli(vault, fake_cli):
    res = t.search("therapy")
    assert res["backend"] == "obsidian-cli"
    assert res["results"][0]["path"] == "Personal/Relationships/Therapy topics.md"
    assert fake_cli()[0][0:2] == ["vault=Trey", "search"]


@pytest.mark.parametrize("mode", ["empty", "error"])
def test_search_falls_back_when_cli_fails(vault, fake_cli, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    res = t.search("therapy")
    assert res["backend"] == "filesystem"
    assert res["results"][0]["path"] == "Personal/Relationships/Therapy topics.md"


def test_app_down_never_calls_cli(vault, fake_cli, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_APP_CHECK", "false")
    from conftest import _reset

    _reset()
    res = t.search("therapy")
    assert res["backend"] == "filesystem" and "not running" in res["note"]
    assert fake_cli() == []  # the CLI would have launched a second app


# --- §8.3 daily note capture ----------------------------------------------------------


def test_daily_note_from_template_and_capture(vault, tmp_path):
    day = t.daily_note("2026-11-11")
    assert day["created"] and day["path"] == "Personal/Daily/2026-11-11.md"
    assert day["frontmatter"]["created"] == "2026-11-11"
    assert "# Wednesday, November 11, 2026" in day["content"]
    t.append_to_note(day["path"], "- buy cat litter", heading="Capture")
    text = (vault / day["path"]).read_text()
    assert "## Capture\n- buy cat litter\n\n## To do" in text
    t.append_to_note(day["path"], "- call vet", heading="Capture")
    assert (
        "## Capture\n- buy cat litter\n- call vet\n\n## To do"
        in (vault / day["path"]).read_text()
    )
    again = t.daily_note("2026-11-11")
    assert not again["created"]
    assert [e["tool"] for e in audit(tmp_path)] == [
        "daily_note",
        "append_to_note",
        "append_to_note",
    ]


def test_daily_note_today_uses_cli(vault, fake_cli, monkeypatch):
    monkeypatch.setenv("VAULT_TZ", "UTC")
    from conftest import _reset

    _reset()
    day = t.daily_note()
    assert day["backend"] == "obsidian-cli"
    assert day["path"] == f"Personal/Daily/{date.today().isoformat()}.md"


def test_append_missing_heading_lists_headings(vault):
    with pytest.raises(ToolError, match="Rug Options"):
        t.append_to_note("Rug Options", "x", heading="Nope")


# --- §8.4 rename --------------------------------------------------------------------


def _check_renamed(vault):
    new = vault / "Home" / "Living Room Rug Options.md"
    assert new.exists() and not (vault / "Home" / "Rug Options.md").exists()
    fm, _, _ = _notes.split(new.read_text())
    assert fm["aliases"] == ["Rug Options"]
    shopping = (vault / "Home" / "Shopping.md").read_text()
    assert "[[Living Room Rug Options|a rug]]" in shopping
    assert "[[Living Room Rug Options#Wool]]" in shopping


def test_rename_filesystem_fallback(vault):
    res = t.move_note("Rug Options", "Living Room Rug Options")
    assert res["backend"] == "filesystem" and res["alias_added"] == "Rug Options"
    _check_renamed(vault)


def test_rename_via_cli_fixes_links_obsidian_missed(vault, fake_cli):
    res = t.move_note("Rug Options", "Living Room Rug Options")
    assert res["backend"] == "obsidian-cli"
    assert any(c[1] == "rename" for c in fake_cli())
    _check_renamed(vault)


def test_move_to_folder_and_dry_run(vault):
    plan = t.move_note("Vex", "People/", dry_run=True)
    assert (
        plan["dry_run"]
        and plan["to"] == "People/Vex.md"
        and plan["alias_added"] is None
    )
    assert (vault / NPCS / "Vex.md").exists()
    t.move_note("Vex", "People/")
    assert (vault / "People" / "Vex.md").exists()


def test_move_with_many_linkers_needs_confirmation(vault):
    for i in range(12):
        (vault / f"L{i}.md").write_text("[[Rug Options]]")
    res = t.move_note("Rug Options", "Rugs")
    assert res["dry_run"] and len(res["notes_with_links_to_update"]) == 13
    assert (vault / "Home" / "Rug Options.md").exists()
    t.move_note("Rug Options", "Rugs", dry_run=False)
    assert (vault / "L0.md").read_text() == "[[Rugs]]"


# --- §8.5 bulk ----------------------------------------------------------------------


def test_bulk_update_is_dry_run_first(vault, tmp_path):
    args = {
        "set_properties": {"status": "archive"},
        "type": "list",
        "updated_before": "2023-01-01",
    }
    plan = t.bulk_update_properties(**args)
    assert plan["dry_run"] and plan["to_change"] == 2
    assert "status: active" in (vault / "Home" / "Old list.md").read_text()
    assert audit(tmp_path) == []
    t.bulk_update_properties(**args, dry_run=False)
    assert "status: archive" in (vault / "Home" / "Old list.md").read_text()
    assert "status: active" in (vault / "Home" / "Shopping.md").read_text()
    assert audit(tmp_path)[0]["tool"] == "bulk_update_properties"


def test_bulk_requires_a_filter(vault):
    with pytest.raises(ToolError, match="filter"):
        t.bulk_update_properties({"status": "archive"})


# --- create / update / delete ----------------------------------------------------------


def test_create_note_conventions(vault):
    res = t.create_note("Weekend idea", "some text")
    assert res["path"] == "_Inbox/Weekend idea.md"
    fm, body, _ = _notes.split((vault / res["path"]).read_text())
    assert fm["type"] == "note" and fm["status"] == "active" and fm["tags"] == []
    assert len(fm["created"]) == 10 and body.startswith("# Weekend idea\n\nsome text")
    home = t.create_note("Paint colors", folder="Home", type="list")
    assert home["frontmatter"]["up"] == "[[Home]]"
    assert 'up: "[[Home]]"' in (vault / home["path"]).read_text()
    with pytest.raises(ToolError, match="already exists"):
        t.create_note("Weekend idea")
    with pytest.raises(ToolError, match="type must be"):
        t.create_note("Bad", type="recipes")
    warn = t.create_note("Tagged", properties={"tags": ["recipe"]})
    assert "theme list" in warn["warnings"][0]


def test_update_note_patch_and_properties(vault):
    t.update_note("Rug Options", patch=[{"find": "jute 8x10", "replace": "jute 9x12"}])
    t.update_note("Rug Options", properties={"status": "archive", "up": None})
    text = (vault / "Home" / "Rug Options.md").read_text()
    assert "jute 9x12" in text and "status: archive" in text and "up:" not in text
    assert "created: 2022-05-01" in text  # dates stay strings, unquoted
    with pytest.raises(ToolError, match="exactly once"):
        t.update_note("Rug Options", patch=[{"find": "- ", "replace": "* "}])


def test_body_edit_keeps_frontmatter_byte_exact(vault):
    before = (vault / "Home" / "Shopping.md").read_text()
    t.update_note("Shopping", body="new body\n")
    after = (vault / "Home" / "Shopping.md").read_text()
    assert after == _notes.split_raw(before)[0] + "new body\n"


def test_delete_via_cli_goes_to_trash(vault, fake_cli):
    res = t.delete_note("Old list")
    assert res["backend"] == "obsidian-cli" and res["moved_to"] == "Obsidian trash"
    assert (vault / ".trash" / "Old list.md").exists()
    assert not any("permanent" in c for call in fake_cli() for c in call)


def test_delete_fallback_goes_to_review(vault):
    res = t.delete_note("Rug Options")
    assert res["moved_to"] == "_Review/To delete/Home/Rug Options.md"
    assert (vault / "_Review/To delete/Home/Rug Options.md").exists()
    assert "1 notes still link" in res["warning"]


# --- safety -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../outside", "/etc/passwd", "Home/../../x", "~/x", ".obsidian/app", ".git/x"],
)
def test_unsafe_paths_rejected(vault, path):
    with pytest.raises(ToolError):
        t.create_note("x", folder=path)


def test_symlink_out_of_vault_rejected(vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("zqxsecret")
    (vault / "link").symlink_to(outside)
    (vault / "secret.md").symlink_to(outside / "secret.md")
    with pytest.raises(ToolError, match="no note found"):
        t.read_note("link/secret.md")
    with pytest.raises(ToolError, match="no note found"):
        t.read_note("secret")
    assert t.search("zqxsecret")["count"] == 0
    with pytest.raises(ToolError, match="leaves the vault"):
        t.create_note("x", folder="link")


def test_tasks_filesystem_and_cli(vault, fake_cli, monkeypatch):
    cli = t.tasks()
    assert cli["backend"] == "obsidian-cli"
    assert cli["tasks"][0] == {
        "path": "Home/Rug Options.md",
        "line": 9,
        "status": " ",
        "text": "measure",
    }
    monkeypatch.setenv("FAKE_CLI_MODE", "error")
    res = t.tasks(status="all")
    assert res["backend"] == "filesystem"
    assert {x["text"] for x in res["tasks"]} == {"measure", "lamp"}


def test_vocabulary_tools(vault):
    assert t.list_tags()["tags"]["eberron"] == 1
    props = t.list_properties()
    assert props["values"]["type"]["list"] == 3
    assert t.get_backlinks("Sam")["backlinks"][0]["title"] == "Therapy topics"


def test_health(vault, fake_cli):
    h = t.health()
    assert (
        h["ok"]
        and h["cli"] == "ok"
        and h["notes"]
        == len([k for k in __import__("conftest").FILES if k.endswith(".md")])
    )


def test_fs_move_does_not_relink_to_a_same_named_note(vault):
    (vault / "Ideas.md").write_text("root ideas")
    t.move_note("Rug Options", "Home/Rugs/Ideas")
    shopping = (vault / "Home" / "Shopping.md").read_text()
    assert "[[Home/Rugs/Ideas|a rug]]" in shopping


def test_empty_find_rejected(vault):
    with pytest.raises(ToolError, match="empty"):
        t.update_note(
            "Rug Options", patch=[{"find": "", "replace": "x", "replace_all": True}]
        )


def test_write_refuses_when_file_changed_since_read(vault):
    from vault_mcp._vault import VaultError

    with pytest.raises(VaultError, match="changed while"):
        t._vault().write_text("Home/Home.md", "new", expect="stale text")


@pytest.mark.parametrize(
    ("out", "want"),
    [('=> "disconnected"', "disconnected"), ('"synced"', "synced"), ("=> null", None)],
)
def test_sync_status_reads_eval_output(monkeypatch, out, want):
    from vault_mcp import _cli, _ready

    monkeypatch.setattr(_cli, "run", lambda *_a, **_k: out + "\n")
    assert _ready.sync_status(None) == want


def test_sync_status_prefers_documented_command(monkeypatch):
    from vault_mcp import _cli, _ready

    real = "status: syncing\nvault: Trey\nvault size: 90.61 MB\n"
    calls = []

    def run(_s, command, *_a, **_k):
        calls.append(command)
        if command == "sync:status":
            return real
        return '=> "fallback"'

    monkeypatch.setattr(_cli, "run", run)
    assert _ready.sync_status(None) == "syncing" and calls == ["sync:status"]
    real = "Error: unknown command"
    assert _ready.sync_status(None) == "fallback"
