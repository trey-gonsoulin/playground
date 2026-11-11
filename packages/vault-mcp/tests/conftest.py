import json
import sys
from pathlib import Path

import pytest
from vault_mcp import _cli, mcp_server
from vault_mcp.config import get_settings

FAKE_CLI = Path(__file__).with_name("fake_obsidian_cli.py")

FILES = {
    ".obsidian/daily-notes.json": json.dumps(
        {
            "folder": "Personal/Daily",
            "format": "YYYY-MM-DD",
            "template": "_Templates/Daily Note",
        }
    ),
    ".obsidian/templates.json": json.dumps(
        {"folder": "_Templates", "dateFormat": "YYYY-MM-DD"}
    ),
    "_Templates/Daily Note.md": (
        "---\ntype: journal\nstatus: active\ncreated: {{date}}\ntags: [daily]\n---\n"
        "# {{date:dddd, MMMM D, YYYY}}\n\n## Capture\n- \n\n## To do\n- [ ] \n\n## Journal\n\n"
    ),
    "Dashboard.md": "---\ntype: index\n---\n# Dashboard\n[[Home]] [[TTRPG]]\n",
    "Home/Home.md": "---\ntype: index\nstatus: active\n---\n# Home\n",
    "Home/Rug Options.md": (
        "---\ntype: list\nstatus: active\ncreated: 2022-05-01\ntags: []\n"
        'up: "[[Home]]"\n---\n# Rug Options\n\n- jute 8x10\n- wool\n\n- [ ] measure\n'
    ),
    "Home/Shopping.md": (
        "---\ntype: list\nstatus: active\nupdated: 2024-02-01\n---\n"
        "Need [[Rug Options|a rug]] and see [[Rug Options#Wool]].\n- [x] lamp\n"
    ),
    "Home/Old list.md": "---\ntype: list\nstatus: active\ncreated: 2021-01-01\n---\nold\n",
    "TTRPG/TTRPG.md": "---\ntype: index\n---\n# TTRPG\n",
    "TTRPG/Murder on the Eberron Express/Murder on the Eberron Express.md": (
        "---\ntype: campaign\nstatus: active\ntags: [eberron-express]\n---\n"
        "# Murder on the Eberron Express\nThe killer is [[Vex]].\n"
    ),
    "TTRPG/Murder on the Eberron Express/NPCs/Vex.md": (
        '---\ntype: campaign\nstatus: active\nup: "[[Murder on the Eberron Express]]"\n---\n'
        "# Vex\nA changeling. Secretly the killer. #eberron\n"
    ),
    "People/Sam Rivera.md": (
        "---\ntype: person\nstatus: active\naliases:\n  - Sam\n  - Sammy\n---\n# Sam\n"
    ),
    "Personal/Relationships/Therapy topics.md": (
        '---\ntype: journal\nstatus: active\npeople:\n  - "[[Sam Rivera]]"\n---\n'
        "# Therapy topics\n\nThings to bring to my therapy session:\n- boundaries\n"
    ),
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Trey"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    monkeypatch.setenv("VAULT_ROOT", str(root))
    monkeypatch.setenv("VAULT_MCP_AUDIT_LOG", str(tmp_path / "audit.log"))
    monkeypatch.setenv("OBSIDIAN_CLI", "")
    monkeypatch.setenv("OBSIDIAN_APP_CHECK", "")
    monkeypatch.delenv("VAULT_MCP_READONLY", raising=False)
    _reset()
    yield root
    _reset()


@pytest.fixture
def fake_cli(vault, tmp_path, monkeypatch):
    """Turn the CLI on, backed by fake_obsidian_cli.py; yields the call log reader."""
    log = tmp_path / "cli.log"
    log.touch()
    monkeypatch.setenv("OBSIDIAN_CLI", f"{sys.executable} {FAKE_CLI}")
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    _reset()

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


def _reset():
    get_settings.cache_clear()
    mcp_server._vault.cache_clear()
    _cli._app_state.clear()


def audit(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
