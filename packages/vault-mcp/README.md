# vault-mcp

An always-on Obsidian vault for Claude (web, desktop and iPhone) through a custom connector.
It's built from `~/Downloads/obsidian-vault-connector-brief.md`.

```
iPhone / Mac / PC ──Obsidian Sync──┐
                                   ▼
EC2 t4g.small (Ubuntu 24.04, no inbound ports, SSM for shell)
  obsidian.service     Obsidian 1.15 under Xvfb :99, built-in Sync, CLI on
  vault-mcp.service    this package, 127.0.0.1:8765, bearer token
  cloudflared          named tunnel ──► https://<hostname>/mcp ◄── claude.ai connector
  vault-backup.timer   nightly git bundle ──► S3 (versioned)
  vault-healthcheck    every 2 min; SNS email after 10 min down
```

## Tools

| Tool | Backend |
|--|--|
| `search(query, folder?, limit?)` | Obsidian search via the CLI (operators work). Falls back to file search if the app is down or returns nothing. |
| `read_note(note, offset?, limit?)` | Files. Returns the body (paged by line), frontmatter as JSON, links and backlinks. |
| `list_notes(folder?, type?, status?, tag?, person?, updated_before?, updated_after?)` | Files (property index). |
| `get_links`, `get_backlinks`, `list_tags`, `list_properties` | Files. |
| `tasks(status?, folder?)` | CLI `tasks`, falling back to parsing the files. |
| `daily_note(day?)` | Reads the note, or creates it: CLI `daily` for today, otherwise from `_Templates/Daily Note`. |
| `create_note`, `update_note`, `append_to_note` | Files, following the Vault Guide (see below). |
| `move_note(note, new, dry_run?)` | CLI `rename`/`move`, then checks on disk and fixes any links left behind. With the app down, moves the file and rewrites links itself. |
| `delete_note(note)` | CLI `delete` to Obsidian's trash. With the app down, moves to `_Review/To delete/`. Never permanent. |
| `bulk_update_properties(set_properties, filters…, dry_run=true)` | Files. Always returns a plan first. |
| `health()` | App, CLI, note count, last change. |

Every result says which `backend` it used.

**Conventions enforced**
- New notes go to `_Inbox` with `type`, `status: active`, `created` and `tags: []`. A note in a top-level area folder gets `up: "[[Folder]]"`.
- `type`, `status` and `source` must be from the Vault Guide's vocabulary. Off-list tags produce a warning.
- A rename adds the old title to `aliases`.
- Body edits never rewrite the frontmatter. `updated` is never touched, because it records an import's last edit.

**Safety**
- Paths resolve inside the vault: no `..`, absolute paths or symlinks out, and no writes in `.obsidian`, `.trash` or `.git`.
- Every write is appended to `~/.local/state/vault-mcp/audit.log` (JSON lines, rotated monthly).
- `VAULT_MCP_READONLY=1` removes the write tools from the tool list.
- The server refuses to start without `VAULT_MCP_TOKEN`.

**Why the CLI isn't trusted blindly:** it always exits 0, prints errors on stdout, and its JSON shapes aren't documented. So the server checks what the CLI did on disk and falls back to the filesystem when the output looks wrong. It also checks that `obsidian.service` is active before every CLI call, because the CLI would otherwise launch a second, unsupervised app.

## Local development

```bash
uv run --package vault-mcp pytest packages/vault-mcp/tests/   # fixture vault + a fake obsidian-cli
VAULT_ROOT=~/Obsidian/Trey VAULT_MCP_TOKEN=dev OBSIDIAN_CLI= OBSIDIAN_APP_CHECK= \
  uv run --package vault-mcp vault-mcp                          # http://127.0.0.1:8765/mcp
```

`OBSIDIAN_CLI=` (empty) means filesystem only. For local writes, use a *copy* of the vault.

## Runbook

`deploy/deploy.sh` does everything from the Mac over SSM, so you never need SSH.
- One-time setup: `brew install --cask session-manager-plugin`.
- Stack: `obsidian-vault` in us-east-1, profile `personal`.

### 1. Mac: move to Obsidian Sync (you, brief §5)
1. Subscribe to Sync (Standard).
2. First shrink or move the files over 5 MB (listed in the brief).
3. Move the vault out of iCloud, to `~/Obsidian/Trey`, and open it in Obsidian.
4. Set these in Settings:
   - Files & links:
     - Default location for new notes → `_Inbox`
     - Attachment folder → `_Attachments`
     - Automatically update internal links → **on**
     - New link format → shortest
     - Deleted files → **Move to Obsidian trash (.trash folder)**
   - Templates: folder → `_Templates`
   - Daily notes:
     - folder → `Personal/Daily`
     - template → `_Templates/Daily Note`
     - format → `YYYY-MM-DD`
5. Settings → Sync:
   - Create a remote vault (end-to-end encryption is fine; keep the password handy).
   - Turn on "Sync all other types" plus images, PDFs and every settings toggle.
   - Wait for the upload to finish.
6. iPhone and PC: create a new **local** vault, then connect it to the remote vault.

### 2. Infrastructure
```bash
cd packages/vault-mcp/deploy
ALERT_EMAIL=you@example.com ./deploy.sh infra   # creates a change set; nothing is executed yet
aws cloudformation execute-change-set --change-set-name <arn> --profile personal --region us-east-1
aws cloudformation wait stack-create-complete --stack-name obsidian-vault --profile personal --region us-east-1
```
- Confirm the SNS subscription email.
- The instance, its volume and the bucket are `Retain`, so deleting the stack doesn't delete your data.

### 3. Server
```bash
./deploy.sh token     # makes the bearer token (SSM SecureString /obsidian-vault/mcp-token) and prints it
./deploy.sh push      # builds the wheels, installs everything, prints status and /healthz
```

### 4. One-time Sync login (VNC through SSM; no XQuartz needed)
1. Run `./deploy.sh vnc`. It prints a password and forwards `localhost:5900`.
2. In Finder, press Cmd+K and connect to `vnc://localhost:5900`.
3. In the Obsidian window:
   - Open the vault `/home/obsidian/vaults/Trey` if it isn't already open.
   - Settings → Sync: log in, connect to the remote vault and enter the encryption password.
   - Turn on the same file-type toggles as on the Mac.
   - Settings → General: turn **Automatic updates off**. App updates are manual (see Ops).
   - Settings → General: check that **Command line interface** is on.
4. Wait for the first sync. Then press Ctrl+C, run `./deploy.sh shell`, and run `sudo -iu obsidian vault-vnc stop`.

### 5. Check the CLI on the box (`./deploy.sh shell`, then `sudo -iu obsidian`)
```bash
obsidian-cli help                                  # compare with the command list in the brief
obsidian-cli vault=Trey vault info=name
obsidian-cli vault=Trey search query=Eberron format=json
obsidian-cli vault=Trey tasks todo verbose format=json | head
```
The JSON shapes of `search` and `tasks` aren't documented. The server accepts a list of paths or of `{path|file, ...}` objects, and anything else falls back to files.
- If the real output differs, adjust `_parse_cli_paths` / `_cli_tasks` in `mcp_server.py`.
- Then edit a note on the Mac and check that it appears on the box, and the other way round.

### 6. Cloudflare Tunnel
1. Cloudflare Zero Trust → Networks → Tunnels → create a tunnel (cloudflared).
2. Add a public hostname, e.g. `vault.<your-domain>`, with service `http://localhost:8765`.
3. Copy the token from the install command, then:
   ```bash
   ./deploy.sh tunnel-token <token>
   ./deploy.sh public-url https://vault.<your-domain>
   ./deploy.sh push
   ```
4. Optional: add a WAF rate-limiting rule on the hostname, e.g. 120 requests/minute per IP. Don't turn on Bot Fight Mode or a managed challenge: Claude's servers can't solve one.

### 7. Claude
Go to claude.ai → Customize → Connectors → Add custom connector:
- Name: "Obsidian Vault"
- URL: `https://vault.<domain>/mcp`
- Request header: `Authorization: Bearer <token from ./deploy.sh token>`

Then run the acceptance tests (brief §8) from the iPhone.

## Ops

- **Logs:** run `./deploy.sh logs`. It shows the journal for all services and the last audit entries.
- **Code update:** run `./deploy.sh push`. This restarts `vault-mcp` only.
- **Obsidian update:** run `OBSIDIAN_VERSION=1.15.1 ./deploy.sh push`. It installs `/opt/obsidian-<v>`, repoints `/opt/obsidian` and restarts the app. To roll back, push the old version.
- **Read-only:** run `./deploy.sh readonly 1 && ./deploy.sh push`.
- **Rotate the token:**
  1. `aws ssm delete-parameter --name /obsidian-vault/mcp-token --profile personal`
  2. `./deploy.sh token && ./deploy.sh push`
  3. Update the connector header.
- **Restore a backup:** run `aws s3 cp s3://<bucket>/backups/vault.bundle . --profile personal` (add `--version-id` for an older night), then `git clone vault.bundle Trey-restore`.
- **Alerts:**
  - The on-box check emails after 10 minutes of app, CLI, MCP or tunnel failure, and again on recovery.
  - CloudWatch recovers or reboots the instance if its status checks fail, and emails too.
