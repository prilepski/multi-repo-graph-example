#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Graph viewer server: serves the HTML viewer and a JSON API over the
Neo4j code graph populated by index_to_neo4j.py.

Endpoints:
  GET /                       -> viewer page (graph-viewer.html)
  GET /api/root               -> topmost nodes (Repository) with child counts
  GET /api/children?eid=      -> hierarchy children (lazy tree expansion)
  GET /api/neighborhood?eid=  -> node + 1-hop neighbors/edges (canvas)
  GET /api/details?eid=       -> node properties + relationship counts
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from neo4j import GraphDatabase

# multi-repo-graph default: this project's own container (mrg-neo4j-instance,
# bolt 7688) — override with env vars to point elsewhere (e.g. java-graph's
# scip-neo4j-instance on 7687).
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7688")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "graphpassword123")
PORT = int(os.environ.get("PORT", "7600"))
MAX_CHILDREN = 500
MAX_NEIGHBORS_PER_DIRECTION = 150

# Labels that form the navigation tree hierarchy
TREE_LABELS = ["Repository", "Package", "Type", "Method", "Field"]

VIEWER_HTML = Path(__file__).resolve().parent / "graph-viewer.html"


class Api:
    def __init__(self, uri, password):
        self.driver = GraphDatabase.driver(uri, auth=("neo4j", password))

    def root(self, mode="packages"):
        child_rel = ":File" if mode == "files" else ":Package"
        with self.driver.session() as s:
            return [dict(r) for r in s.run(f"""
                MATCH (r:Repository)
                RETURN elementId(r) AS eid, 'Repository' AS label, r.name AS name,
                       count {{ (r)-[:CONTAINS]->({child_rel}) }} AS children
                ORDER BY r.name
            """)]

    def children(self, eid, mode="packages"):
        with self.driver.session() as s:
            parent_label = s.run(
                "MATCH (p) WHERE elementId(p) = $eid RETURN labels(p)[0] AS l",
                eid=eid).single()
            if parent_label is None:
                return {"children": [], "truncated": False}
            parent_label = parent_label["l"]

            if mode == "files" and parent_label == "Repository":
                query = """
                    MATCH (p) WHERE elementId(p) = $eid
                    MATCH (p)-[:CONTAINS]->(c:File)
                    RETURN elementId(c) AS eid, 'File' AS label, c.path AS name,
                           NULL AS kind, count { (c)-[:DEFINES]->() } AS children
                    ORDER BY name LIMIT $cap
                """
            elif mode == "files" and parent_label == "File":
                query = """
                    MATCH (p) WHERE elementId(p) = $eid
                    MATCH (p)-[:DEFINES]->(c:Type)
                    RETURN elementId(c) AS eid, 'Type' AS label, c.name AS name,
                           c.kind AS kind, count { (c)-[:CONTAINS]->() } AS children
                    ORDER BY name LIMIT $cap
                """
            else:
                query = """
                    MATCH (p) WHERE elementId(p) = $eid
                    MATCH (p)-[:CONTAINS]->(c)
                    WHERE c:Package OR c:Type OR c:Method OR c:Field
                    RETURN elementId(c) AS eid,
                           labels(c)[0] AS label,
                           coalesce(c.name, c.fqn, c.id) AS name,
                           c.kind AS kind,
                           count { (c)-[:CONTAINS]->() } AS children
                    ORDER BY label, name
                    LIMIT $cap
                """
            rows = [dict(r) for r in s.run(query, eid=eid, cap=MAX_CHILDREN + 1)]
        truncated = len(rows) > MAX_CHILDREN
        return {"children": rows[:MAX_CHILDREN], "truncated": truncated}

    def ancestors(self, eid, mode="packages"):
        """Hierarchy chain from a node up to its Repository (for tree sync).
        files mode: Repository-CONTAINS->File-DEFINES->Type-CONTAINS->member."""
        rels = ":CONTAINS|DEFINES*1..6" if mode == "files" else ":CONTAINS*1..6"
        with self.driver.session() as s:
            return [dict(r) for r in s.run(f"""
                MATCH (n) WHERE elementId(n) = $eid
                MATCH path = (anc)-[{rels}]->(n)
                RETURN elementId(anc) AS eid, labels(anc)[0] AS label,
                       coalesce(anc.name, anc.fqn, anc.id) AS name,
                       length(path) AS depth
                ORDER BY depth DESC
            """, eid=eid)]

    # evidence-class weights mirror indexer/integrations.py CLASS_WEIGHT
    IG_CLASS_WEIGHT = {"submodule": 100.0, "code": 40.0, "config": 20.0,
                       "comment": 5.0, "docs": 1.0}

    def integrations(self):
        """Cross-repo (:Repository)-[:INTEGRATES {refs, files, byClass,
        strongest, samples}]->() edges, derived from name references in
        the indexed sources, split by evidence class (submodule > code >
        config > comment > docs). `score` weights refs by class so doc
        verbosity cannot drown code-level evidence."""
        import json as _json
        with self.driver.session() as s:
            out = [dict(r) for r in s.run("""
                MATCH (a:Repository)-[r:INTEGRATES]->(b:Repository)
                RETURN a.name AS src, elementId(a) AS srcEid,
                       b.name AS dst, elementId(b) AS dstEid,
                       r.refs AS refs, r.files AS files,
                       r.samples AS samples, r.byClass AS byClass,
                       r.strongest AS strongest
                ORDER BY refs DESC
            """)]
        for row in out:  # containers are stored as JSON strings (neo4j props)
            if isinstance(row.get("samples"), str):
                try:
                    row["samples"] = _json.loads(row["samples"])
                except ValueError:
                    row["samples"] = []
            if isinstance(row.get("byClass"), str):
                try:
                    row["byClass"] = _json.loads(row["byClass"])
                except ValueError:
                    row["byClass"] = {}
            by_class = row.get("byClass") or {}
            if by_class:  # score = Σ refs_c × weight_c; legacy edges: refs
                row["score"] = sum(
                    n * self.IG_CLASS_WEIGHT.get(c, 1.0)
                    for c, n in by_class.items())
            else:
                row["score"] = row.get("refs") or 0
        return out

    def neighborhood(self, eid, depth=1, mode="packages"):
        """1-hop neighborhood of a node; depth 2 additionally expands Type
        neighbors with their EXTENDS/IMPLEMENTS links (cross-file class links).
        Returns {focus, nodes, edges}; edges reference node element ids."""
        with self.driver.session() as s:
            rec = s.run("""
                MATCH (n) WHERE elementId(n) = $eid
                RETURN elementId(n) AS eid, labels(n)[0] AS label,
                       coalesce(n.name, n.fqn, n.id) AS name, n.kind AS kind
            """, eid=eid).single()
            if rec is None:
                return None
            focus = dict(rec)
            nodes: dict = {}
            edges: list = []

            # integrations first: a Repository's cross-repo links must not
            # drown under the child cap (children can hit MAX_NEIGHBORS)
            if focus["label"] == "Repository":
                for r in s.run("""
                    MATCH (n) WHERE elementId(n) = $eid
                    MATCH (n)-[r:INTEGRATES]->(m:Repository)
                    RETURN type(r) AS rel, elementId(m) AS eid,
                           labels(m)[0] AS label, m.name AS name, NULL AS kind,
                           r.refs AS refs, r.strongest AS strongest
                """, eid=eid):
                    n = {k: r[k] for k in ("eid", "label", "name", "kind")}
                    if n["eid"] != focus["eid"] and n["eid"] not in nodes:
                        nodes[n["eid"]] = n
                    label = f"{r['rel']} ({r['refs'] or '?'} refs"
                    if r["strongest"]:
                        label += f" · {r['strongest']}"
                    edges.append({"src": focus["eid"], "dst": n["eid"],
                                  "rel": label + ")"})

            # filter Repository children by browsing mode (File vs Package)
            mode_filter = """
                AND ($mode <> 'files' OR NOT n:Repository OR m:File)
                AND ($mode <> 'packages' OR NOT n:Repository OR NOT m:File)
            """
            for r in s.run(f"""
                MATCH (n) WHERE elementId(n) = $eid
                MATCH (n)-[r]->(m)
                WHERE NOT m:StatsSnapshot AND NOT m:CategoryStat
                  AND NOT n:StatsSnapshot AND NOT n:CategoryStat
                  AND true {mode_filter}
                RETURN type(r) AS rel, elementId(m) AS eid, labels(m)[0] AS label,
                       coalesce(m.name, m.fqn, m.id) AS name, m.kind AS kind
                LIMIT $cap
            """, eid=eid, cap=MAX_NEIGHBORS_PER_DIRECTION, mode=mode):
                n = {k: r[k] for k in ("eid", "label", "name", "kind")}
                if n["eid"] != focus["eid"] and n["eid"] not in nodes:
                    nodes[n["eid"]] = n
                edges.append({"src": focus["eid"], "dst": n["eid"], "rel": r["rel"]})

            for r in s.run(f"""
                MATCH (n) WHERE elementId(n) = $eid
                MATCH (n)<-[r]-(m)
                WHERE NOT m:StatsSnapshot AND NOT m:CategoryStat
                  AND NOT n:StatsSnapshot AND NOT n:CategoryStat
                  AND true {mode_filter}
                RETURN type(r) AS rel, elementId(m) AS eid, labels(m)[0] AS label,
                       coalesce(m.name, m.fqn, m.id) AS name, m.kind AS kind
                LIMIT $cap
            """, eid=eid, cap=MAX_NEIGHBORS_PER_DIRECTION, mode=mode):
                n = {k: r[k] for k in ("eid", "label", "name", "kind")}
                if n["eid"] != focus["eid"] and n["eid"] not in nodes:
                    nodes[n["eid"]] = n
                edges.append({"src": n["eid"], "dst": focus["eid"], "rel": r["rel"]})

            truncated = len(nodes) >= MAX_NEIGHBORS_PER_DIRECTION * 2

            # depth 2: class-level links across files (EXTENDS/IMPLEMENTS)
            if depth >= 2:
                type_eids = [eid for eid, n in nodes.items() if n["label"] == "Type"]
                if focus["label"] == "Type":
                    type_eids = [focus["eid"]] + type_eids
                for te in type_eids[:40]:
                    if len(nodes) >= 400:
                        truncated = True
                        break
                    for r in s.run("""
                        MATCH (t) WHERE elementId(t) = $e
                        MATCH (t)-[r:EXTENDS|IMPLEMENTS]-(m:Type)
                        RETURN elementId(m) AS eid, labels(m)[0] AS label,
                               coalesce(m.name, m.fqn, m.id) AS name, m.kind AS kind,
                               type(r) AS rel, startNode(r) = t AS out
                        LIMIT 25
                    """, e=te):
                        m = {k: r[k] for k in ("eid", "label", "name", "kind")}
                        if m["eid"] == focus["eid"]:
                            continue
                        if m["eid"] not in nodes:
                            if len(nodes) >= 400:
                                truncated = True
                                break
                            m["anchor"] = te
                            nodes[m["eid"]] = m
                        if r["out"]:
                            edges.append({"src": te, "dst": m["eid"], "rel": r["rel"]})
                        else:
                            edges.append({"src": m["eid"], "dst": te, "rel": r["rel"]})

            # annotate Type nodes with the file that defines them
            type_eids = [eid for eid, n in nodes.items() if n["label"] == "Type"]
            if focus["label"] == "Type":
                type_eids.append(focus["eid"])
            if type_eids:
                for r in s.run("""
                    MATCH (f:File)-[:DEFINES]->(t) WHERE elementId(t) IN $eids
                    RETURN elementId(t) AS eid, f.path AS file
                """, eids=type_eids):
                    if r["eid"] in nodes:
                        nodes[r["eid"]]["file"] = r["file"]
                    if r["eid"] == focus["eid"]:
                        focus["file"] = r["file"]

        return {"focus": focus, "nodes": list(nodes.values()), "edges": edges,
                "truncated": truncated}

    # ------------------------------------------------------------------
    # Statistics (inventory) endpoints
    # ------------------------------------------------------------------

    def stats_overview(self, repo=None, prefix=""):
        """KPIs for the statistics pane; scoped when repo+prefix given."""
        with self.driver.session() as s:
            if repo:
                rec = s.run("""
                    MATCH (:Repository {name: $repo})-[:CONTAINS]->(f:File)
                    WHERE f.path STARTS WITH $prefix
                    RETURN count(f) AS tracked,
                           sum(CASE WHEN exists((f)-[:DEFINES]->()) THEN 1 ELSE 0 END) AS deep,
                           sum(CASE WHEN f.kind = 'artifact' OR f.category IN ['image','font','binary_blob']
                                    THEN 1 ELSE 0 END) AS binaries,
                           sum(f.totalLines) AS totalLines, sum(f.codeLines) AS codeLines,
                           sum(f.commentLines) AS commentLines, sum(f.blankLines) AS blankLines
                """, repo=repo, prefix=prefix).single()
                return {"scope": {"repo": repo, "prefix": prefix},
                        "kpis": {"tracked": rec["tracked"], "onDisk": None,
                                 "untracked": None, "deep": rec["deep"],
                                 "inventoryOnly": rec["tracked"] - rec["deep"],
                                 "binaries": rec["binaries"],
                                 "totalLines": rec["totalLines"] or 0,
                                 "codeLines": rec["codeLines"] or 0,
                                 "commentLines": rec["commentLines"] or 0,
                                 "blankLines": rec["blankLines"] or 0},
                        "repos": []}
            repos = []
            for r in s.run("""
                MATCH (repo:Repository)
                OPTIONAL MATCH (repo)-[:CONTAINS]->(f:File)
                RETURN repo.name AS repository, count(f) AS filesInGraph,
                       sum(CASE WHEN exists((f)-[:DEFINES]->()) THEN 1 ELSE 0 END) AS deep,
                       sum(CASE WHEN f.kind = 'artifact' OR f.category IN ['image','font','binary_blob']
                                THEN 1 ELSE 0 END) AS binaries
                ORDER BY repository
            """):
                row = dict(r)
                cov = s.run("""
                    MATCH (sp:StatsSnapshot)-[c:COVERS]->(:Repository {name: $n})
                    WITH sp, c ORDER BY sp.at DESC LIMIT 1
                    RETURN c.filesTracked AS filesTracked, c.filesOnDisk AS filesOnDisk,
                           c.untrackedFiles AS untrackedFiles, c.filesIndexedDeep AS filesIndexedDeep,
                           c.filesInventoryOnly AS filesInventoryOnly, c.binaries AS binaries
                """, n=row["repository"]).single()
                row["coverage"] = dict(cov) if cov else None
                repos.append(row)
            return {"scope": None, "kpis": None, "repos": repos}

    def stats_categories(self, repo=None, prefix="", kind=None):
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                WHERE ($repo IS NULL OR r.name = $repo)
                  AND ($prefix IS NULL OR f.path STARTS WITH $prefix)
                  AND ($kind IS NULL OR f.kind = $kind)
                RETURN f.kind AS kind, f.category AS category,
                       count(f) AS files, sum(f.bytes) AS bytes,
                       sum(f.totalLines) AS totalLines, sum(f.codeLines) AS codeLines,
                       sum(f.commentLines) AS commentLines, sum(f.blankLines) AS blankLines
                ORDER BY files DESC
            """, repo=repo, prefix=prefix or None, kind=kind)]

    def stats_loc(self, repo=None, prefix=""):
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                WHERE ($repo IS NULL OR r.name = $repo)
                  AND ($prefix IS NULL OR f.path STARTS WITH $prefix)
                RETURN r.name AS repository, f.category AS category, count(f) AS files,
                       sum(f.totalLines) AS totalLines, sum(f.codeLines) AS codeLines,
                       sum(f.commentLines) AS commentLines, sum(f.blankLines) AS blankLines
                ORDER BY totalLines DESC
            """, repo=repo, prefix=prefix or None)]
        totals = {k: sum(r[k] or 0 for r in rows)
                  for k in ("files", "totalLines", "codeLines", "commentLines", "blankLines")}
        return {"rows": rows, "totals": totals}

    def stats_tree_children(self, eid):
        """Directory-tree expansion: eid = 'repo:<name>' or 'dir:<repo>:<relpath>'."""
        if eid.startswith("repo:"):
            repo, prefix = eid[5:], ""
        elif eid.startswith("dir:"):
            rest = eid[5:]
            repo, prefix = rest.split(":", 1)
            prefix = prefix + "/"
        else:
            return {"children": [], "truncated": False}
        with self.driver.session() as s:
            paths = [r["path"] for r in s.run("""
                MATCH (:Repository {name: $repo})-[:CONTAINS]->(f:File)
                WHERE f.path STARTS WITH $prefix
                RETURN f.path AS path ORDER BY path
            """, repo=repo, prefix=prefix)]
        dirs, files, seen = {}, [], set()
        rest_len = len(prefix)
        for path in paths:
            rel = path[rest_len:]
            if "/" in rel:
                seg = rel.split("/", 1)[0]
                if seg not in dirs:
                    dirs[seg] = 1
                else:
                    dirs[seg] += 1
            else:
                files.append(rel)
        children = ([{"eid": f"dir:{repo}:{prefix}{d}", "name": d, "isDir": True,
                      "files": n} for d, n in sorted(dirs.items())]
                    + [{"eid": f"file:{repo}:{prefix}{f}", "name": f,
                        "isDir": False, "files": 1} for f in sorted(files)])
        truncated = len(children) > 500
        return {"children": children[:500], "truncated": truncated}

    def stats_files(self, repo=None, prefix="", category=None, limit=500):
        """Files inside one category (drill-down), scoped like the other stats."""
        with self.driver.session() as s:
            base = """
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                WHERE ($repo IS NULL OR r.name = $repo)
                  AND ($prefix IS NULL OR f.path STARTS WITH $prefix)
                  AND ($cat IS NULL OR f.category = $cat)
            """
            total = s.run("RETURN 0").single()  # placeholder to reuse session
            total = s.run(base + " RETURN count(f) AS c",
                          repo=repo, prefix=prefix or None, cat=category).single()["c"]
            rows = [dict(r) for r in s.run(base + """
                RETURN r.name AS repository, f.path AS path, f.extension AS extension,
                       f.role AS role, f.bytes AS bytes, f.totalLines AS totalLines,
                       f.codeLines AS codeLines
                ORDER BY path LIMIT $lim
            """, repo=repo, prefix=prefix or None, cat=category, lim=limit)]
        return {"category": category, "total": total, "files": rows,
                "truncated": total > len(rows)}

    def search(self, q):
        """Case-insensitive substring search over declared classes and file paths."""
        q = q.strip().lower()
        if len(q) < 2:
            return {"types": [], "files": []}
        with self.driver.session() as s:
            types = [dict(r) for r in s.run("""
                MATCH (t:Type {external: false})
                WHERE toLower(t.name) CONTAINS $q
                OPTIONAL MATCH (f:File)-[:DEFINES]->(t)
                RETURN elementId(t) AS eid, t.name AS name, t.fqn AS fqn,
                       t.kind AS kind, f.path AS file
                LIMIT 20
            """, q=q)]
            files = [dict(r) for r in s.run("""
                MATCH (f:File)
                WHERE toLower(f.path) CONTAINS $q
                RETURN elementId(f) AS eid, f.path AS path
                LIMIT 20
            """, q=q)]
        return {"types": types, "files": files}

    def details(self, eid):
        with self.driver.session() as s:
            rec = s.run("""
                MATCH (n) WHERE elementId(n) = $eid
                RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props
            """, eid=eid).single()
            if rec is None:
                return None
            result = dict(rec)
            result["relations"] = [dict(r) for r in s.run("""
                MATCH (n) WHERE elementId(n) = $eid
                MATCH (n)-[r]-()
                RETURN type(r) AS type, count(r) AS count ORDER BY count DESC
            """, eid=eid)]
            return result


API = Api(NEO4J_URI, NEO4J_PASSWORD)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = VIEWER_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # sibling static assets (force-graph.min.js) — this dir only, no ".."
        if "/" not in url.path[1:] and "." in url.path[1:]:
            asset = VIEWER_HTML.parent / url.path[1:]
            if asset.is_file():
                ctype = ("application/javascript" if asset.suffix == ".js"
                         else "application/octet-stream")
                body = asset.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if url.path == "/api/integrations":
            return self._send_json(API.integrations())
        if url.path == "/api/root":
            mode = parse_qs(url.query).get("mode", ["packages"])[0]
            return self._send_json(API.root(mode))
        if url.path == "/api/children":
            qs = parse_qs(url.query)
            eid = qs.get("eid", [None])[0]
            mode = qs.get("mode", ["packages"])[0]
            if not eid:
                return self._send_json({"error": "eid required"}, 400)
            return self._send_json(API.children(eid, mode))
        if url.path == "/api/ancestors":
            qs = parse_qs(url.query)
            eid = qs.get("eid", [None])[0]
            mode = qs.get("mode", ["packages"])[0]
            if not eid:
                return self._send_json({"error": "eid required"}, 400)
            return self._send_json(API.ancestors(eid, mode))
        if url.path == "/api/neighborhood":
            qs = parse_qs(url.query)
            eid = qs.get("eid", [None])[0]
            mode = qs.get("mode", ["packages"])[0]
            try:
                depth = int(qs.get("depth", ["1"])[0])
            except ValueError:
                depth = 1
            if not eid:
                return self._send_json({"error": "eid required"}, 400)
            data = API.neighborhood(eid, depth, mode)
            if data is None:
                return self._send_json({"error": "node not found"}, 404)
            return self._send_json(data)
        if url.path == "/api/search":
            q = parse_qs(url.query).get("q", [""])[0]
            return self._send_json(API.search(q))
        if url.path == "/api/stats/overview":
            qs = parse_qs(url.query)
            return self._send_json(API.stats_overview(
                repo=qs.get("repo", [None])[0], prefix=qs.get("prefix", [""])[0]))
        if url.path == "/api/stats/categories":
            qs = parse_qs(url.query)
            return self._send_json(API.stats_categories(
                repo=qs.get("repo", [None])[0], prefix=qs.get("prefix", [""])[0],
                kind=qs.get("kind", [None])[0]))
        if url.path == "/api/stats/loc":
            qs = parse_qs(url.query)
            return self._send_json(API.stats_loc(
                repo=qs.get("repo", [None])[0], prefix=qs.get("prefix", [""])[0]))
        if url.path == "/api/stats/files":
            qs = parse_qs(url.query)
            return self._send_json(API.stats_files(
                repo=qs.get("repo", [None])[0], prefix=qs.get("prefix", [""])[0],
                category=qs.get("category", [None])[0]))
        if url.path == "/api/stats/tree/children":
            qs = parse_qs(url.query)
            eid = qs.get("eid", [None])[0]
            if not eid:
                return self._send_json({"error": "eid required"}, 400)
            return self._send_json(API.stats_tree_children(eid))
        if url.path == "/api/details":
            eid = parse_qs(url.query).get("eid", [None])[0]
            if not eid:
                return self._send_json({"error": "eid required"}, 400)
            data = API.details(eid)
            if data is None:
                return self._send_json({"error": "node not found"}, 404)
            return self._send_json(data)
        return self._send_json({"error": "not found"}, 404)


def main():
    import os
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    host = os.environ.get("VIEWER_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Viewer running at http://{host}:{port} "
          f"(Neo4j {NEO4J_URI})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
