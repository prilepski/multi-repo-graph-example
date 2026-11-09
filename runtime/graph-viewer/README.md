# Multi Repo Code Graph Viewer

Interactive viewer for the portfolio code graph stored in Neo4j. The graph
is produced by the multi-repo-graph pipeline (codegraph extraction →
portfolio schema); this project only reads it.

Rendering is [force-graph](https://github.com/vasturiano/force-graph)
(WebGL, vendored as `force-graph.min.js` — no build step; the workspace
runtime ships it as-is).

## Features

- **Constellation canvas** — glowing orbs, curved links, animated
  particles on integration edges, pan/zoom (drag/wheel), deterministic
  ring layout for the repo overview and radial seeding for neighborhoods
- **Cross-repo integration view** — at the root, repos sit on a ring with
  amber `INTEGRATES` chords (width ∝ class-weighted score, particles ∝
  traffic); hover a chord for `src → dst · N refs (M files)` plus the
  per-class evidence breakdown
- **Unified filter bar** (bottom-right) — `Nodes | Edges` mode switch;
  chips show the categories present in the CURRENT view with counts and
  graph colors:
  - *nodes*: node labels (Repository/Package/Type/…) — the old legend,
    now toggleable
  - *edges*: at the root the INTEGRATES evidence classes
    (Submodule/Code/Config/Comment/Docs); drilled in, the relationship
    types (CONTAINS/DEFINES/CALLS/EXTENDS/…)
  - instant apply (node/link visibility — no re-layout), persisted per
    browser (`fg-nodes-off` / `fg-edges-off`), evidence-class preferences
    migrate from the legacy `ig-classes` key
- **Repository multi-select** (header, root view) — choose which repos the
  constellation shows; persisted via `fg-repos-off` (stores the
  deselected, so new repos appear by default); the slot becomes the
  breadcrumb when drilled
- **Animation toggle** — link particles on/off, persisted (`fg-anim`)
- **Two browsing modes** — Packages and Files hierarchies
- **Collapsible tree** synced with canvas navigation
- **Click** a node → details popup (properties, relationship counts);
  **double-click** → drill into its neighborhood; **↑ Level up** walks back
- **Search** files by path and classes by name
- **Statistics surface** — coverage KPIs, category donut, LOC breakdowns,
  scoped by clicking the stats tree (reads the inventory layer:
  `:File` taxonomy props + `:StatsSnapshot`/`:CategoryStat`)

## API (viewer_server.py)

| Endpoint | Purpose |
|---|---|
| `GET /` | this page |
| `GET /force-graph.min.js` (and sibling assets) | vendored static assets |
| `GET /api/root?mode=packages\|files` | repositories with child counts |
| `GET /api/children?eid=` | tree expansion |
| `GET /api/neighborhood?eid=&mode=&depth=` | node + 1-hop (depth 2 adds class links); excludes StatsSnapshot/CategoryStat |
| `GET /api/details?eid=` | properties + relationship counts |
| `GET /api/integrations` | INTEGRATES edges with refs/files/byClass/strongest/score/samples |
| `GET /api/stats/*` | statistics surface |

Config via env: `NEO4J_URI` (default `bolt://localhost:7688` — this
project's workspace consumer), `NEO4J_PASSWORD`, `PORT` (default 7600).
