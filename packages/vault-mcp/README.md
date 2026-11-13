# vault-mcp

The Obsidian vault as a claude.ai custom connector, from `~/Downloads/obsidian-vault-connector-brief.md`.
It runs **on demand**: the box sleeps when Claude isn't using it, and your devices keep syncing through Obsidian Sync as usual.

```
iPhone / Mac / PC ──Obsidian Sync (always)──┐
                                            ▼
claude.ai ──► API Gateway + Lambda ──HTTPS:8443──► EC2 t4g.small (only while in use)
              (vault_mcp.proxy,     (box's own CA,   obsidian.service  Obsidian 1.15 under Xvfb, Sync, CLI
               always on, stable    bearer token)    vault-mcp         this package, the real tools
               URL)                                  vault-idle-stop   stops the box after 30 idle min,
                 │                                                     once Sync has uploaded everything
                 └─ box stopped? StartInstances → "starting, call start_vault()"
```

**How a request flows:**
1. Claude calls a tool on the Lambda.
2. If the box is asleep, the Lambda starts it and answers "starting; call `start_vault()`".
3. `start_vault()` polls until the box is up *and* Obsidian Sync has caught up (see "Readiness" below). That takes about 1–3 minutes.
4. Calls then go straight through.
5. After 30 minutes without an MCP call, the box waits for Sync to finish uploading Claude's edits, takes a local snapshot, and powers off. A power-off stops the instance; it never terminates it.

**Cost:** about $3/month at a couple of hours of use a day.
- The 20 GB gp3 volume is ~$1.60/month whether the box runs or not.
- The t4g.small is ~$0.017/hour while running, and its public IPv4 address ~$0.005/hour while running.
- Lambda, API Gateway and SNS cost pennies.

## Tools

The Lambda lists the box's tools with identical schemas. It wraps the same functions, so the two lists can't drift apart.

| Tool | Backend |
|--|--|
| `start_vault()` | Lambda only. Wakes the box and waits up to ~20 s per call until it's ready. |
| `health()` | Lambda only. Reports the instance state, plus the box's health while it's running. Doesn't wake the box. |
| `search(query, folder?, limit?)` | Obsidian search via the CLI, so operators work. Falls back to file search. |
| `read_note(note, offset?, limit?)` | Files. Returns the body (paged by line), frontmatter as JSON, links and backlinks. |
| `list_notes(folder?, type?, status?, tag?, person?, updated_before?, updated_after?)` | Files (property index). |
| `get_links`, `get_backlinks`, `list_tags`, `list_properties` | Files. |
| `tasks(status?, folder?)` | CLI `tasks`, falling back to parsing the files. |
| `daily_note(day?)` | Reads the note, or creates it: CLI `daily` for today, otherwise from `_Templates/Daily Note`. |
| `create_note`, `update_note`, `append_to_note` | Files, following the Vault Guide. |
| `move_note(note, new, dry_run?)` | CLI `rename`/`move`, checked on disk. Fixes any links Obsidian left behind and keeps the old title as an alias. |
| `delete_note(note)` | CLI `delete` to Obsidian's trash. With the app down, moves to `_Review/To delete/`. |
| `bulk_update_properties(set_properties, filters…, dry_run=true)` | Files. Always returns a plan first, and takes a snapshot before applying. |
| `create_notes(notes, dry_run?)` | Files. Up to 200 notes per call, each as in `create_note`. Applies up to 10 at once; more return a plan first. |
| `update_notes(updates, dry_run=true)` | Files. Up to 200 edits (body, patches, properties), each as in `update_note`. The plan shows a diff per note; a snapshot is taken before applying. |
| `move_notes(moves, dry_run=true)` | As `move_note`, for up to 200 notes. Applying stops after ~15 s and returns the rest as `remaining` to call again with. |
| `import_archive(url, folder, …, sha256?, dry_run=true)` | Downloads a zip or tar from an https URL (100 MiB, 2000 files) into a folder. Applying needs the plan's `sha256`. |

Every box result names its `backend`.

**Batch tools**
- Each batch checks every item before writing anything; if any item fails, nothing is written and the errors are listed by index.
- `import_archive` never overwrites: existing files are listed as conflicts and skipped.
  - It skips hidden entries (`.obsidian/`, `__MACOSX/`), links, `..` paths and files over 5 MB (Sync Standard's limit).
  - It adds missing `type`/`status`/`created`/`tags` to notes and keeps existing properties.
  - It only fetches public hosts, so a URL can't reach the instance metadata service.
  - Sources that work: a Dropbox link with `dl=1`, the Dropbox connector's temporary download link, or a GitHub archive URL.
- There's no base64 upload: Claude would have to emit the whole archive as tokens, which costs far more than `create_notes` with plain text.

**Conventions enforced**
- New notes go to `_Inbox` with `type`, `status: active`, `created` and `tags: []`.
- `type`, `status` and `source` must be from the Vault Guide's vocabulary.
- A rename adds the old title to `aliases`.
- Frontmatter edits rewrite only the changed keys; every other line stays byte-for-byte.
- `updated` is never touched.

**Safety**
- Paths stay inside the vault: no `..`, absolute paths or symlinks out, and no writes in `.obsidian`, `.trash` or `.git`.
- Writes are serialized, and a write is refused if the note changed since it was read.
- Every write is audit-logged (JSON lines, rotated monthly).
- `readonly` removes the write tools from both tool lists.
- Both the Lambda and the box refuse to start without the token.
- Port 8443 is public, as with soulsearch's OpenSearch port, so the box uses TLS from its own CA plus the bearer token. The Lambda pins that CA through SSM.

**Why the CLI isn't trusted blindly:**
- It always exits 0, prints errors on stdout, and its JSON shapes are undocumented. So the server checks what it did on disk and falls back to the files.
- It also checks that `obsidian.service` is active before every CLI call, because the CLI would otherwise launch a second, unsupervised app.

**Readiness:** "Sync has caught up" means all of the following:
- The app is running.
- Sync's status isn't one of the `sync-busy` names. The status comes from the CLI's `sync:status` (falling back to an `eval` of the Sync plugin's internals). The busy status is `syncing`.
- Nothing in the vault has changed for 30 s. This uses ctime, which Sync can't backdate.
- The box has been up for at least 60 s.

After 10 minutes the box serves anyway. Until it's ready, the box's tools answer "still catching up".

## Local development

```bash
uv run --package vault-mcp pytest packages/vault-mcp/tests/   # fixture vault, fake obsidian-cli, in-process box behind the proxy
VAULT_ROOT=~/Obsidian/Trey VAULT_MCP_TOKEN=dev OBSIDIAN_CLI= OBSIDIAN_APP_CHECK= \
  uv run --package vault-mcp vault-mcp                          # http://127.0.0.1:8765/mcp (no TLS unless VAULT_MCP_SSL_CERT)
```

`OBSIDIAN_CLI=` (empty) means filesystem only. For local writes, use a *copy* of the vault.

## Runbook

`deploy/deploy.sh` does everything from the Mac.

**Prerequisites:**
- the AWS CLI with profile `personal`
- the SAM CLI and Docker Desktop
- `brew install --cask session-manager-plugin`

**Defaults:** stack `obsidian-vault` in us-east-1, configured in `deploy/samconfig.toml`, with the AMI pinned there.

### 1. Mac: move to Obsidian Sync (you, brief §5)
1. Subscribe to Sync (Standard).
2. Shrink or move the files over 5 MB (listed in the brief).
3. Move the vault out of iCloud to `~/Obsidian/Trey`, and open it in Obsidian.
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
   - Create a remote vault (end-to-end encryption is fine).
   - Turn on "Sync all other types", images, PDFs and the settings toggles.
   - Wait for the upload to finish.
6. iPhone and PC: create a new **local** vault, then connect it to the remote vault.

### 2. Stack
```bash
cd packages/vault-mcp/deploy
./deploy.sh token          # bearer token (SSM SecureString /obsidian-vault/mcp-token); keep the printed header
./deploy.sh infra          # creates the ECR repo if needed, sam build, and a change set (nothing executed)
aws cloudformation execute-change-set --change-set-name <arn> --profile personal --region us-east-1
aws cloudformation wait stack-create-complete --stack-name obsidian-vault --profile personal --region us-east-1
./deploy.sh alerts you@example.com   # then confirm the subscription email
```

### 3. Box
```bash
./deploy.sh push           # starts the box if needed, installs everything, prints /healthz
```
A push counts as activity, so the box stays up for the next 30 minutes.

### 4. One-time Sync login (VNC through SSM; no XQuartz needed)
1. Run `./deploy.sh vnc`. It prints a password, forwards `localhost:5900`, and holds the idle stop.
2. In Finder, press Cmd+K and connect to `vnc://localhost:5900`.
3. In Obsidian:
   - Open the vault `/home/obsidian/vaults/Trey` if it isn't open.
   - Settings → Sync: log in, connect to the remote vault, enter the encryption password, and set the same file-type toggles as on the Mac.
   - Settings → General: turn **Automatic updates off** and check that **Command line interface** is on.
4. Wait for the first full sync.

### 5. Check the CLI and Sync status (`./deploy.sh shell`, then `sudo -iu obsidian`)
```bash
obsidian-cli help
obsidian-cli vault=Trey search query=Eberron format=json
obsidian-cli vault=Trey tasks todo verbose format=json | head
obsidian-cli vault=Trey sync:status
```
- Run `sync:status` while Sync is idle, then again while it's busy (edit a note on the Mac). Busy reads `status: syncing`.
- If the busy value isn't one of `syncing,uploading,downloading,connecting,pending`, run `./deploy.sh sync-busy "<values>"` and then `./deploy.sh push`.
- The server accepts `search`/`tasks` output as a list of paths or of `{path|file, ...}` objects; anything else falls back to the files. On CLI 1.12, search gives a list of paths and tasks gives `{status, text, file, line}`.
- When done, run `vault-vnc stop` (still as `obsidian`) to release the idle hold.

### 6. Claude
1. Run `./deploy.sh url` to get the connector URL.
2. Go to claude.ai → Customize → Connectors → Add custom connector:
   - Name: "Obsidian Vault"
   - URL: the one from step 1
   - Request header: `Authorization: Bearer <token>`
3. Run the brief's acceptance tests (§8) from the iPhone. Add one more: leave the box idle for 30+ minutes, edit a note on the phone, then ask Claude about that note. It should start the box, wait for Sync, and see the edit.

## Ops

- **Logs:** run `./deploy.sh logs`. It starts the box if needed.
- **Start or keep up by hand:**
  - `./deploy.sh start` starts the box.
  - On the box, `touch ~obsidian/.local/state/vault-mcp/hold` keeps it up; remove the file to release it.
- **Idle time:** `./deploy.sh idle-minutes 60 && ./deploy.sh push`.
- **Code updates:**
  - Box: `./deploy.sh push`.
  - Lambda: `./deploy.sh infra`, then execute the change set.
- **Obsidian update:** `OBSIDIAN_VERSION=1.15.1 ./deploy.sh push`. To roll back, push the old version.
- **Read-only:** `./deploy.sh readonly 1 && ./deploy.sh push`. The Lambda picks it up on its next cold start.
- **Rotate the token:**
  1. `aws ssm delete-parameter --name /obsidian-vault/mcp-token --profile personal`
  2. `./deploy.sh token && ./deploy.sh push`
  3. Update the connector header. Warm Lambdas pick up the new token within 5 minutes.
- **Undo a bad bulk edit:** Obsidian Sync's version history keeps 30 days. The box also keeps local snapshots, taken before every bulk change and every idle stop:
  - Browse: `sudo -iu obsidian git --git-dir ~/snapshots/vault.git log --stat`
  - Restore: `git --git-dir ~/snapshots/vault.git --work-tree ~/vaults/Trey checkout <commit> -- <path>`
- **Alerts:**
  - The box emails if it has been unhealthy for 10 minutes. It only checks after its first 15 minutes up.
  - It emails if it had to stop before Sync settled.
  - CloudWatch recovers or reboots the instance if its status checks fail while it's running.
