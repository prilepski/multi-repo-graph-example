# MFLens-compatible MCP Server

Implements the complete 60-tool MFLens inventory
(`mflens-tool-inventory.md`) as an MCP server, backed by the **Java code
graph** in Neo4j (populated by [java-indexer](../java-indexer)) and the
cloned repository sources.

## Domain mapping

| MFLens concept | Java-graph equivalent |
|---|---|
| program | Java class / interface / enum / record (`:Type`) |
| portfolio | everything indexed (all repositories) |
| paragraph | method |
| data structure | field |
| lineage | extends / implements / derived / called-by edges |
| deep call chain | `Method-CALLS->CallTarget`, resolved best-effort by name |

Tools whose domain does not exist in a Java portfolio — CICS, IMS, MQ, JCL/CL,
COBOL conversion, SQL tables — are registered and return the documented empty
shapes (mirroring the inventory's live results). The three mutating tools
write to a **local `annotations.json`**, never to the graph.

**Performance & drift detection**

- `search_code_portfolio` uses **ripgrep** when available (PATH or the bundled
  ZCode location) and falls back to a parallel (8-worker) file scan —
  measured ~2x faster warm (~390 ms vs ~710 ms sequential over 9,516 files).
- Every `:File` node carries `sha256` (written by java-indexer). The
  source-reading tools (`get_program_source_code`, `search_code`,
  `get_program_paragraph_details`) return `fileHashMatch`: `false` means the
  file on disk has drifted from the version the graph was built from;
  `null` means no hash is stored (pre-backfill run).

## Layout

| File | Purpose |
|---|---|
| `server.py` | FastMCP server: the 60-tool MFLens inventory + 4 inventory-statistics extension tools (`ALL_TOOLS`, 64 total) |
| `backend.py` | `Neo4jBackend` (live) + `FakeBackend` (offline tests), shared interface |
| `tests/test_mcp_tools.py` | 111 tests: inventory completeness, Tier A/B/C behavior, mutators, protocol |
| `annotations.json` | local store for the mutating tools (created on first write) |

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# stdio (register in .zcode/.mcp.json as a command):
.venv/bin/python server.py

# streamable HTTP on 127.0.0.1:8790/mcp:
MCP_TRANSPORT=http .venv/bin/python server.py
```

Configuration (env): `MCP_NEO4J_URI` (default `bolt://localhost:7687`),
`MCP_NEO4J_PASSWORD` (default `graphpassword123`), `MCP_REPOS_JSON`
(default `../java-indexer/repos.json` — maps repositories to local clone
paths for source reads). `MCPLENS_FAKE=1` selects the offline fixture
backend (used by the tests).

Example client registration (stdio):

```json
{
  "mcpServers": {
    "MFlens": {
      "command": "<abs-path>/mcp-server/.venv/bin/python",
      "args": ["<abs-path>/mcp-server/server.py"]
    }
  }
}
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

111 tests, fully offline (fixture portfolio with real source files):
- **Inventory**: exactly the 60 documented tools registered, names match
  1:1, every tool has a description
- **Tier A**: search resolution (name + FQN), exact physical-line source
  reads, file/portfolio text search with correct lines, deep call chain
  (internal resolved, cross-type resolved, external leaf without `nodeType`)
- **Tier B**: overview/files/assets/complexity counts, export shape,
  accurate method spans (tree-sitter re-parse), CFG edges, paged call
  chain with node typing, call paths, ibmi-usage resolution flags,
  lineage sources/targets/trace (direction semantics), field types,
  SQL-family documented-empty shapes
- **Tier C**: dead paragraphs (graph-computed), non-broken call-chain
  statistics (no empty names), portfolio usage aggregation (not zero),
  empty legacy outputs/field-lineage/capabilities, platform tools empty,
  JCL not-found, COBOL refusal, computed modernization/impact
- **Mutators**: summary/PRD/classification round-trips through the local
  annotation store; auto-generated fallbacks when unset
- **Protocol**: `list_tools` over the MCP protocol returns the 64 names;
  a tool call round-trips through the protocol layer
- **Extension tools** (inventory & code statistics, backed by the java-indexer
  inventory phase): `get_portfolio_file_statistics` (coverage per repository
  from the latest StatsSnapshot + live facts), `get_file_categories`,
  `get_loc_statistics` (code/comment/blank), `get_file_details` — all
  scopeable by repository / path prefix / category. Protocol smoke:
  `scripts/call_all_tools.py` (64 calls, live graph).
