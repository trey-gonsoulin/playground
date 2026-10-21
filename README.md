# playground

Personal MCP servers, deployed to AWS Lambda and used from Claude Desktop and Claude iOS. Each server is a package under `packages/` in one uv workspace:

- `elden-ring` searches Elden Ring game data at every patch, from an OpenSearch index.
- `genealogy` builds a family tree from WikiTree, Library of Congress newspapers and GEDCOM files.
- `ynab-mcp` is a YNAB budget client.

`genealogy` and `ynab-mcp` deploy into one SAM stack, `mcp-playground`, through a reviewed change set. The Elden Ring Lambda and its OpenSearch instance deploy from soulsearch's `soulsearch` stack (soulsearch#2, soulsearch#3). `CLAUDE.md` covers the shared layout, the commands, the deploy procedure and each package. The Elden Ring runbook (reindex, index cutover, tests) is in `packages/elden-ring/README.md`; its deploy and instance rebuild steps are in soulsearch.
