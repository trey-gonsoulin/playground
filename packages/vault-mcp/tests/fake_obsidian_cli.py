"""Stand-in for ``obsidian-cli`` with the real CLI's quirks: exit code always 0,
errors printed to stdout as ``Error: ...``. Moves do NOT rewrite links, so the
server's own link fixing is exercised. Commands are appended to $FAKE_CLI_LOG.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

root = Path(os.environ["VAULT_ROOT"])
args = sys.argv[1:]
with Path(os.environ["FAKE_CLI_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\n")
params = dict(a.split("=", 1) for a in args if "=" in a)
command = next(a for a in args if "=" not in a)
mode = os.environ.get("FAKE_CLI_MODE", "ok")

if mode == "error":
    print("Error: something went wrong")
elif command == "vault":
    print(root.name)
elif command == "search":
    q = params["query"].lower()
    hits = [
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*.md"))
        if ".obsidian" not in p.parts and q in p.read_text().lower()
    ]
    print(json.dumps([] if mode == "empty" else hits))
elif command == "rename":
    src = root / params["path"]
    src.rename(src.with_name(params["name"] + ".md"))
elif command == "move":
    dst = root / params["to"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    (root / params["path"]).rename(dst)
elif command == "delete":
    trash = root / ".trash"
    trash.mkdir(exist_ok=True)
    src = root / params["path"]
    src.rename(trash / src.name)
elif command == "daily":
    p = root / "Personal" / "Daily" / f"{date.today().isoformat()}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: journal\n---\n# from obsidian\n\n## Capture\n- \n")
elif command == "tasks":
    print(
        json.dumps(
            [
                {  # the real CLI's shape (1.12): file, line as text, whole line
                    "status": " ",
                    "text": "- [ ] measure",
                    "file": "Home/Rug Options.md",
                    "line": "9",
                }
            ]
        )
    )
else:
    print(f"Error: unknown command {command}")
