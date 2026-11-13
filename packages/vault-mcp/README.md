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
| `read_note(note, offset?, limit?, heading?)` | Files. Returns the body (paged by line, or one heading's section), typed frontmatter, `version` (sha256), links and backlinks. |
| `list_notes(folder?, type?, status?, tag?, person?, updated_before?, updated_after?)` | Files (property index). |
| `get_links`, `get_backlinks`, `list_tags`, `list_properties` | Files. |
| `tasks(status?, folder?)` | CLI `tasks`, falling back to parsing the files. |
| `daily_note(day?)` | Reads the note, or creates it: CLI `daily` for today, otherwise from `_Templates/Daily Note`. |
| `create_note`, `update_note`, `append_to_note` | Files, following the Vault Guide. `create_note` can start from a template (`template`, plus `patch` to fill it in). The edits take `if_version`. |
| `move_note(note, new, dry_run?)` | CLI `rename`/`move`, checked on disk. Fixes any links Obsidian left behind and keeps the old title as an alias. |
| `delete_note(note)` | CLI `delete` to Obsidian's trash. With the app down, moves to `_Review/To delete/`. |
| `bulk_update(set_properties?, patch?, filters…, where?, query?, paths?, dry_run=true)` | Files. Sets properties and/or patches the body of every matching note (at most 500). Always returns a plan first and takes a snapshot before applying; all or nothing. |
| `create_notes(notes, dry_run?)` | Files. Up to 200 notes per call, each as in `create_note`. Applies up to 10 at once; more return a plan first. |
| `update_notes(updates, dry_run=true)` | Files. Up to 200 edits (body, patches, properties), each as in `update_note`. The plan shows a diff per note; a snapshot is taken before applying. |
| `move_notes(moves, dry_run=true)` | As `move_note`, for up to 200 notes. Applying stops after ~15 s and returns the rest as `remaining` to call again with. |
| `import_archive(url, folder, …, sha256?, dry_run=true)` | Downloads a zip or tar from an https URL (100 MiB, 2000 files) into a folder. Applying needs the plan's `sha256`. |
| `read_file(path, offset?, length?)` | Files, byte for byte, with the sha256. Text as UTF-8, anything else as base64; 1 MB per call. |
| `write_file(path, content, if_absent \| if_version, more?, sha256?)` | Files, byte for byte: no frontmatter or heading changes. Big files go in chunks (`more=true`), staged outside the vault and written once at the end. 5 MB max. |
| `validate(schema, folder?, type?, where?)` | Files. Reports missing required properties, values outside an allowed set and wrong property types. |
| `unresolved_links(folder?)` | Files. Every link target that points at no note or file, with counts and the notes holding it. |
| `query_base(base, view?)` | CLI `base:query`, for a `.base` file or a note embedding one. Adds the view's summaries (Sum, Average, …), per group too. Inline `base` code blocks can't be queried. |
| `git_status`, `git_diff`, `git_log`, `git_show`, `git_commit` | The snapshot repo (`~/snapshots/vault.git`, work tree = the vault). |
| `begin_write(owner, reason, paths?, folders?)`, `end_write(lock, owner, message)` | A write lock for one session. `end_write` commits the locked paths as one commit and releases the lock. |
| `run_script(name, args?)` | Runs a script from `_Meta/scripts` only if its sha256 is pinned in SSM `scripts`. |

Every box result names its `backend`.

**Several sessions writing**
- `read_note`, `read_file` and every write return the file's `version` (sha256) and size. Pass `version` as `if_version` (in `update_note`, `append_to_note`, `update_notes` items and `write_file`) to refuse a write if the note changed in between. The refusal includes the current text.
- `begin_write` locks notes or folders. Writes to them without the lock are refused, and writes naming the lock must stay inside it.
  - The lock lives in `locks.json` next to the audit log and expires after `minutes` (max 240).
  - `end_write(…, break_lock=true)` releases someone else's lock without committing. This is logged.
  - `run_script` is refused while another session holds a lock, since a script can write anywhere.
  - While any lock is live, the snapshot before a bulk edit or script commits everything except the locked paths, so a batch stays one `end_write` commit.
  - `end_write` and `git_commit` skip paths the repo's `.gitignore` leaves out and list them as `ignored`.
- The git tools use the snapshot repo, so `git_status` shows everything since the last commit, whether that was a snapshot, `git_commit` or `end_write`. That includes edits Sync brought in from other devices.

**Types**
- `read_note` returns frontmatter typed as Obsidian reads it (YAML 1.2 core): `10.10` is the number 10.1, `"10.10"` is text and dates stay text.
- Writes keep JSON types: numbers and booleans go out plain, and text that would read as a number or boolean is quoted (`menu-index: "10.10"`).
- A write is refused if it gives a property a type that no other note uses for it. For example, `characters: 81` is refused when every other note has a list. This only applies to properties with a single type across at least two other notes.

**Patches:** each one has `find` (literal) or `find_regex` (Python, multiline). It must match exactly once, unless `replace_all` is set or `count` gives the exact number of matches expected.

**Scripts:** to approve one, look up its sha256 (`run_script` reports it when it refuses), then:
```bash
aws ssm put-parameter --name /obsidian-vault/scripts --type String --overwrite \
  --value 'elden-ring-characters.py=<sha256>' --profile personal --region us-east-1
./deploy.sh push
```
- A script runs as `<script> <vault root> [args…]`: `.py` with `python3 -I`, `.sh` with `bash`.
- It runs from a copy of the exact bytes that were checked, with a scrubbed environment and a temporary `HOME`. Small files left in that `HOME` come back as `outputs`.
- It still runs as the `obsidian` user, which can read the server's files, token included. Only approve a script you've read.
- It's limited to ~20 s, after a snapshot.
- Editing a script un-approves it.

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
