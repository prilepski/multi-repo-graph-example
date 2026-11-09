<!-- mrg:keep-readme -->
# multi-repo-graph-example

A **populated sample [codegraph-workspace](https://github.com/prilepski/multi-repo-graph)**:
everything the multi-repo-graph batch indexer produces in ONE full cycle
(clone → codegraph index → Neo4j portfolio graph → verified snapshot →
deployed runtime), run over two well-known Apache-2.0 repositories.

## What's indexed

| Repo | What it is | Scale (approx.) |
|---|---|---|
| [thingsboard](https://github.com/thingsboard/thingsboard) | IoT platform — Java backend + TS UI | ~2.1M LOC, ~14k files |
| [dolphinscheduler](https://github.com/apache/dolphinscheduler) | workflow scheduler — Java + TS | ~0.45M LOC, ~4.9k files |

Together they exercise the interesting parts of a portfolio graph: two
repos, mixed languages, shared nothing (clean namespacing) — and the
cross-repo `INTEGRATES` edges with evidence classes derived from
name references.

## What's inside

```
workspace.json    the repos + settings used by the indexer (this sample's input)
runs/<ts>/        timestamped snapshots: run-manifest.json (per-repo commit SHAs,
                  LOC, artifact SHAs, verification status), summary.json,
                  neo4j/{neo4j,system}.dump.zst (graph + credentials),
                  codegraphs/<repo>/codegraph.db.zst (per-repo codegraph DBs)
latest.json       pointer to the newest run
runtime/          deployed viewer + MCP servers, pinned by runtime/VERSION
```

Artifacts are zstd-compressed and stored via **git-lfs** — clone with lfs
installed (`git lfs install`).

## Consume it (no tooling needed)

    runtime/scripts/up.sh        # Windows: runtime/scripts/up.ps1
                                 # needs only Python 3.11+ (+ Docker, Node 22.5+)

That restores the latest snapshot and starts:

- **Multi Repo Code Graph Viewer** at http://localhost:7620 — the
  constellation of both repos with cross-repo integration chords
  (evidence-class colored, animated particles), the Nodes/Edges filter bar
  (bottom-right), repo multi-select (header), and a Statistics surface
  (file taxonomy, LOC breakdowns, coverage)
- **Neo4j** at http://localhost:7701 (bolt :7700, password in
  workspace.json) — the portfolio graph, queryable directly
- **MCP servers** — paste `runtime/config/mcp.json` into your
  ZCode/Claude config: MFLens (60+4-tool portfolio-graph contract) and
  codegraph-portfolio (fan-out search/callers/impact/explore)

Stop with `runtime/scripts/down.sh` (add `--purge` to drop the restored
volume). Repo sources are checked out at the exact SHAs recorded in the
run manifest.

## How this sample was produced

With the (private) multi-repo-graph tool checkout:

    indexer/.venv/bin/python -m indexer.pipeline \
        --workspace /path/to/this/repo

One command runs stages 0–8 and commits the run (plus the runtime) back
into this repo. `summary.json` in each run records the full verification
report; `run-manifest.json` records exactly what was indexed, at which
commit, with which tool versions and schema fingerprint.

## Purpose

- a **reference** for what a healthy workspace looks like (layout,
  manifests, verification)
- a **playground** for the viewer and MCPs without indexing anything
  yourself
- an **end-to-end example** of the tool → workspace separation: this repo
  is fully self-contained; the tool repo is never needed to consume it
