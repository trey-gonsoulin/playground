# playground

Personal MCP servers, deployed to AWS Lambda and used from Claude Desktop and Claude iOS. Each server is a package under `packages/` in one uv workspace:

- `elden-ring` searches Elden Ring game data at every patch, from an OpenSearch index.
- `genealogy` builds a family tree from WikiTree, Library of Congress newspapers and GEDCOM files.
- `therapist-finder` searches Inclusive Therapists and Psychology Today.
- `ynab-mcp` is a YNAB budget client.

All four deploy into one SAM stack, `mcp-playground`, through a reviewed change set. `CLAUDE.md` covers the shared layout, the commands, the deploy procedure and each package. The Elden Ring runbook (reindex, index cutover, tests, deploy, instance rebuild) is in `packages/elden-ring/README.md`.
