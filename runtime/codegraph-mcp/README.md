# codegraph-mcp — portfolio fan-out wrapper (design.md §10)

MCP server that fans codegraph queries out over **every repo's** codegraph
index and merges the results, each tagged with the repo it came from.
Complements — never replaces — the MFLens 60+4 contract (`../mcp-server/`):
MFLens answers portfolio-graph questions over Neo4j (types, members,
inheritance, name-level CALLS); this server answers codegraph questions
(FTS search, resolved callers, impact radius, task context) across repos.

Implemented per design.md §10 ("search/callers/impact/explore across all
repos"), ahead of the snapshot-to-local runner (it reads the working-tree
`.codegraph/` indexes the pipeline builds).

## Layout

| File | Purpose |
|---|---|
| `fanout.mjs` | Node CLI: opens each repo's index read-only, runs one command, prints merged JSON. Resolves the codegraph SDK from `indexer/nodejs/node_modules` (single pinned install, no duplicate). |
| `server.py` | FastMCP server (`mcp<2`), 5 tools, each shelling to `fanout.mjs`. |
| `repos.json` | Portfolio repos (`../repos/*`, same format as MFLens repos.json) |
| `tests/` | 21 tests: fan-out merge/tagging/isolation over TWO indexed fixture repos + MCP tool contracts |

## Tools

| Tool | What it does |
|---|---|
| `portfolio_search(query, limit=20)` | FTS symbol search, all repos |
| `portfolio_symbol(qualified_name)` | exact identity lookup (survives re-index) |
| `portfolio_callers(name, repo="")` | resolved callers, all overloads, per repo |
| `portfolio_impact(name, depth=2, repo="")` | impact radius per repo |
| `portfolio_explore(query, max_nodes=40)` | codegraph task-context per repo |

Every response: `{command, repos, failures, results}` — a repo whose index
is missing/unreadable appears in `failures` and never fails the call.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py          # stdio MCP
# repos config: MRG_REPOS_JSON (default repos.json here); node: MRG_NODE
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q          # 21 (indexes real fixture repos)
```

Multi-repo cases covered: same simple name in two repos stays distinct
(repo-tagged), repo filter narrows the fan-out, an unindexed repo is
isolated into `failures`, TS + Java mixed, and MCP tool contracts.
