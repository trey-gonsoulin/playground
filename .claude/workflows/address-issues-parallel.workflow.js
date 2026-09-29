export const meta = {
  name: 'address-issues-parallel',
  description: 'Implement several elden-ring GitHub issues in parallel worktrees, then independently review each branch (implement -> review -> optional fix + re-review)',
  whenToUse: 'Called by /address-issue --parallel. The coordinator merges, ships and closes out after this returns.',
  phases: [
    { title: 'Implement', detail: 'one agent per issue group, isolated worktrees in playground + erdb-tools' },
    { title: 'Review', detail: 'independent reviewer per branch, starts as soon as its branch is pushed' },
    { title: 'Fix', detail: 'only for branches with blocking review problems; one fix round + re-review' },
  ],
}

// args: {
//   groups: [{ key: '168', issues: [168, 171], title: 'short title', context: 'related closed issues,
//             memory files to read, user decisions (binding), design questions' }],
//   scratch: '<coordinator scratchpad dir>'   // per-group subdirs are created under it
// }
// The coordinator scouts inline first (reads the issues, picks memory files, records user decisions)
// and passes that as each group's `context`. Nothing is merged here.

const A = args || {}
const GROUPS = Array.isArray(A.groups) ? A.groups : []
if (!GROUPS.length) return { error: 'args.groups is empty' }
const SCRATCH = A.scratch || '/tmp'

const REPO = 'trey-gonsoulin/playground'
const PLAYGROUND = '/Users/trey/code/playground'
const ERDB = '/Users/trey/code/erdb-tools'
const MEMORY = '/Users/trey/.claude-personal/projects/-Users-trey-code-playground/memory'
const SPOT_BUILD = '/Users/trey/.claude-personal/scripts/er_spot_build.sh'
const ATTRIB = 'Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>'

const branchOf = g => `issue-${g.key}`
const refs = g => g.issues.map(n => `#${n}`).join(', ')
const siblings = g => GROUPS.filter(o => o.key !== g.key).map(o => `${refs(o)} (${o.title})`).join('; ') || 'none'

const COMMON = `
## Repos and worktrees
- **playground** (${PLAYGROUND}): the MCP server. You already run in an isolated git worktree of it; make all playground edits there. Relevant: packages/elden-ring/elden_ring/_client.py (INDEX_MAPPING is dynamic: strict, so every new field needs a mapping entry, plus the _FIELD_NOTES used by describe_fields), mcp_server.py, and packages/elden-ring/tests/.
- **erdb-tools** (${ERDB}): the extraction code (er_native/extract.py etc.). NEVER edit its main checkout, because other agents share it. Create your own worktree (see Rules for the branch name) and edit only there. Gitignored data (datalake/, unpack/, regulation*.decrypted, ss-venv/) exists only in the main checkout: read it by absolute path, never write to it.

## How to test and build
- erdb-tools tests are plain scripts, not pytest: \`${ERDB}/ss-venv/bin/python er_native/test_<name>.py\` prints OK. Run them all (er_native/test_*.py) before reporting.
- playground: \`uv run --package elden-ring pytest packages/elden-ring/tests/\`, \`uv run ruff check\` and \`uv run ruff format --check\`.
- Spot builds: \`bash ${SPOT_BUILD} <your erdb-tools worktree> <version> <ABSOLUTE out.json>\`. Build 1.17.0 and 1.02.1 from your worktree BEFORE changing code (the baseline), then again after, and diff. Confirm only the intended docs and fields change, and check every new leaf is covered by INDEX_MAPPING. For historical-patch params see memory feedback-new-param-historical-spot-check.md.
- Cross-check values against the Fextralife wiki (WebFetch) where it has numbers.

## Memory
Read ${MEMORY}/MEMORY.md, then the memory files the issue context names, plus feedback-single-version-build-inputs.md, feedback-soulstruct-param-todict-defaults.md, feedback-soulstruct-er-tooltips-ds1.md and feedback-new-field-mapping-before-test-index.md. Don't edit memory.

## Working style
- Lean context: trim tool output (head/grep), write big probe output to files and print only summaries, and read large files in parts.
- Sandbox: chained \`cd … && git …\`, shell for-loops, heredocs and \`VAR=… cmd\` prefixes can be refused. Put probe scripts in files, run plain commands, and use \`git -C <path>\`.
- The user pre-approved this work: don't enter plan mode or ask questions. Make sensible design calls and record them. Any change to user-visible wording (e.g. an existing \`effect\` string) or a naming choice the user might care about goes in \`user_visible_changes\` / \`decisions\` with needs_user=true. Follow the binding user decisions in the issue context exactly.
`.trim()

const RULES = g => `
## Rules
- Branch \`${branchOf(g)}\` in both repos. erdb-tools: \`git -C ${ERDB} worktree add ${ERDB}-wt/${branchOf(g)} -b ${branchOf(g)} main\`. playground: push your worktree HEAD as \`${branchOf(g)}\`.
- Commit messages in the repos' \`type(scope): desc (${refs(g)})\` style, ending with the line "${ATTRIB}". Push both branches (\`git push -u origin ${branchOf(g)}\` / \`git push origin HEAD:${branchOf(g)}\`).
- Do NOT merge to main, reindex, deploy, touch OpenSearch, close or file issues, or edit memory. The coordinator does all of that.
- Keep the diff minimal, localized and in the surrounding style. Sibling agents are editing the same files concurrently for: ${siblings(g)}.
- Scratch files go under ${SCRATCH}/a${g.key}/.
`.trim()

const implPrompt = g => `
You are one of ${GROUPS.length} parallel agents, each working GitHub issues end to end in isolated worktrees. Your issue(s): **${refs(g)}: ${g.title}** (repo ${REPO}, label elden-ring). Read each with \`gh issue view <N> --repo ${REPO} --comments\`.

## Issue context from the coordinator
${g.context || '(none)'}

${COMMON}

${RULES(g)}

## What to do
Read, understand, implement with tests, spot-build and diff, commit, push. Your final answer is the structured report: fill every field with concrete numbers (docs changed at 1.17 / 1.02, example values vs the wiki).
`.trim()

const reviewPrompt = (g, r) => `
You are an independent reviewer for branch \`${branchOf(g)}\` (${refs(g)}: ${g.title}, repo ${REPO}). Another agent implemented it; its report is below. Your job is to find what's wrong, not to confirm it. Default to "fix" only for real, evidenced problems.

## Implementer's report
${JSON.stringify(r, null, 2)}

## Issue context from the coordinator
${g.context || '(none)'}

## Check
1. Read the issue(s) (\`gh issue view <N> --repo ${REPO} --comments\`) and the diffs: \`git -C ${ERDB} diff main...origin/${branchOf(g)}\` and \`git -C ${PLAYGROUND} diff main...origin/${branchOf(g)}\` (fetch first).
2. Does the change do what the issue asks, and follow every binding user decision in the context?
3. Rerun the tests in the implementer's worktrees (paths in the report). Don't edit them.
4. Verify the spot-build claims: rerun its diff or rebuild 1.17 with \`bash ${SPOT_BUILD} <erdb worktree> 1.17.0 <abs out>\` into ${SCRATCH}/r${g.key}/. Confirm that only the intended fields changed, and independently check 2–3 values against the params or the wiki.
5. Mapping: is every new field mapped with a sensible type, and are the field notes accurate?
6. Look for regressions in unrelated docs, unreported changes to user-visible wording, and code that silently skips cases.
Do NOT commit, push, or edit either branch. Use lean context: trim output and keep probe output in files.
`.trim()

const fixPrompt = (g, r, rv) => `
You implemented branch \`${branchOf(g)}\` (${refs(g)}: ${g.title}). An independent review found blocking problems. Fix them in your existing worktrees (erdb-tools: ${r.worktrees.erdb_tools || 'none'}; playground: ${r.worktrees.playground || 'none'}), rerun all tests and the spot-build diff, commit and push to \`${branchOf(g)}\` in both repos, then return an updated full report.

## Review problems
${JSON.stringify(rv.problems, null, 2)}

## Your previous report
${JSON.stringify(r, null, 2)}

${COMMON}

${RULES(g)}
`.trim()

const STR = { type: 'string' }
const REPORT = {
  type: 'object',
  required: ['branches', 'worktrees', 'changes', 'mapping_changes', 'tests', 'spot_build', 'user_visible_changes', 'decisions', 'closing_comments', 'gaps', 'memory_findings'],
  properties: {
    branches: { type: 'object', required: ['erdb_tools', 'playground'], properties: {
      erdb_tools: { type: 'object', properties: { branch: STR, sha: STR } },
      playground: { type: 'object', properties: { branch: STR, sha: STR } } } },
    worktrees: { type: 'object', properties: { erdb_tools: STR, playground: STR } },
    changes: { type: 'string', description: 'functions, fields and notes changed (markdown)' },
    mapping_changes: { type: 'array', items: { type: 'object', required: ['field', 'type'], properties: { field: STR, type: STR } } },
    tests: { type: 'string', description: 'commands run and results' },
    spot_build: { type: 'object', required: ['summary'], properties: {
      docs_changed_117: { type: 'integer' }, docs_changed_102: { type: 'integer' },
      summary: { type: 'string', description: 'fields changed, examples checked against the wiki' } } },
    user_visible_changes: { type: 'array', description: 'changed wording of existing text fields, before -> after',
      items: { type: 'object', required: ['item', 'before', 'after'], properties: { item: STR, before: STR, after: STR } } },
    decisions: { type: 'array', items: { type: 'object', required: ['decision', 'needs_user'], properties: {
      decision: STR, rationale: STR, needs_user: { type: 'boolean' } } } },
    closing_comments: { type: 'array', items: { type: 'object', required: ['issue', 'body'], properties: {
      issue: { type: 'integer' }, body: { type: 'string', description: 'markdown closing comment: what was done, what remains and why' } } } },
    gaps: { type: 'array', items: { type: 'object', required: ['title', 'kind'], properties: {
      title: STR, hint: STR, kind: { type: 'string', enum: ['actionable', 'data_limited', 'existing_issue'] },
      existing_issue: { type: 'integer' } } } },
    memory_findings: { type: 'array', items: STR },
  },
}
const REVIEW = {
  type: 'object',
  required: ['verdict', 'problems', 'checks_run'],
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fix'] },
    problems: { type: 'array', items: { type: 'object', required: ['severity', 'description'], properties: {
      severity: { type: 'string', enum: ['blocking', 'minor'] }, description: STR, evidence: STR } } },
    checks_run: { type: 'string' },
  },
}

log(`Implementing ${GROUPS.length} group(s): ${GROUPS.map(refs).join(' | ')}`)

const results = await pipeline(
  GROUPS,
  g => agent(implPrompt(g), { label: `impl:${g.key}`, phase: 'Implement', isolation: 'worktree', agentType: 'general-purpose', schema: REPORT }),
  (report, g) => report
    ? agent(reviewPrompt(g, report), { label: `review:${g.key}`, phase: 'Review', agentType: 'general-purpose', schema: REVIEW })
        .then(review => ({ report, review }))
    : null,
  async (r, g) => {
    if (!r) return { group: g, error: 'implementer returned nothing' }
    const blocking = (r.review?.problems || []).filter(p => p.severity === 'blocking')
    if (r.review?.verdict !== 'fix' || !blocking.length) return { group: g, ...r }
    log(`${refs(g)}: ${blocking.length} blocking review problem(s), running one fix round`)
    const fixed = await agent(fixPrompt(g, r.report, r.review), { label: `fix:${g.key}`, phase: 'Fix', agentType: 'general-purpose', schema: REPORT })
    if (!fixed) return { group: g, ...r, fix_failed: true }
    const rereview = await agent(reviewPrompt(g, fixed), { label: `re-review:${g.key}`, phase: 'Fix', agentType: 'general-purpose', schema: REVIEW })
    return { group: g, report: fixed, review: rereview, first_review: r.review, fixed: true }
  },
)

const out = results.map((r, i) => r || { group: GROUPS[i], error: 'pipeline dropped this group' })
const needsUser = out.filter(r => r.report && ((r.report.user_visible_changes || []).length || (r.report.decisions || []).some(d => d.needs_user)))
log(`Done. Passed review: ${out.filter(r => r.review?.verdict === 'pass').length}/${out.length}; needing the user: ${needsUser.map(r => refs(r.group)).join(', ') || 'none'}`)
return { results: out, needs_user: needsUser.map(r => r.group.key) }
