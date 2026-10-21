# playground

Personal MCP servers, deployed to AWS Lambda and used from Claude Desktop and Claude iOS. Each server is a package under `packages/` in one uv workspace:

- `genealogy` builds a family tree from WikiTree, Library of Congress newspapers and GEDCOM files.
- `ynab-mcp` is a YNAB budget client.

Both deploy into one SAM stack, `mcp-playground`, through a reviewed change set. `CLAUDE.md` covers the shared layout, the commands, the deploy procedure and each package.

The Elden Ring search server moved to [trey-gonsoulin/soulsearch](https://github.com/trey-gonsoulin/soulsearch). Its OpenSearch instance stays in `mcp-playground` until soulsearch#3.
