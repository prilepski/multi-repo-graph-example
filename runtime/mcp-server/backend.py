# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Backends for the MFLens-compatible MCP server.

One interface, two implementations:

- ``Neo4jBackend`` — live data: the Java code graph produced by
  ``java-indexer`` (Repository/Package/Type/Method/Field/File/CallTarget)
  plus the cloned repository sources for anything line- or text-based.
- ``FakeBackend`` — a small in-memory portfolio with real source files on
  disk, used by the offline test-suite.

Domain mapping (documented in README):
  "program"      -> a Java class/interface/enum/record (:Type node)
  "portfolio"    -> everything indexed in the graph (all repositories)
  "paragraph"    -> a method (BEGSR/PERFORM analog)
  "data structure" -> a field
  "lineage"      -> extends/implements/imports/reference edges
"""

import hashlib
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent

# ripgrep candidates: PATH first, then known bundled locations
RG_CANDIDATES = [
    "rg",
    "/Applications/ZCode.app/Contents/Resources/tools/ripgrep/rg",
]


def find_rg() -> Optional[str]:
    for cand in RG_CANDIDATES:
        found = shutil.which(cand)
        if found:
            return found
        p = Path(cand)
        if p.is_file() and p.is_executable():
            return str(p)
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def drift_check(stored_sha: Optional[str], current_text: str) -> Optional[bool]:
    """None = no stored hash (pre-backfill), else True when the file on disk
    still matches the version the graph was built from."""
    if not stored_sha:
        return None
    return stored_sha == sha256_text(current_text)


# ---------------------------------------------------------------------------
# Source-file access helpers (shared)
# ---------------------------------------------------------------------------

def read_lines(root: Path, rel_path: str) -> List[str]:
    path = root / rel_path
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def slice_lines(lines: List[str], start: int, end: int) -> str:
    """1-based inclusive physical line slice."""
    if not lines:
        return ""
    start = max(1, start)
    end = min(len(lines), end if end and end > 0 else len(lines))
    return "\n".join(f"{i:>6}| {lines[i - 1]}" for i in range(start, end + 1))


def text_search(lines: List[str], pattern: str, max_results: int = 100) -> List[dict]:
    hits = []
    rx = re.compile(re.escape(pattern), re.IGNORECASE)
    for idx, line in enumerate(lines, 1):
        if rx.search(line):
            hits.append({"line": idx, "text": line.rstrip()})
            if len(hits) >= max_results:
                break
    return hits


# ---------------------------------------------------------------------------
# Neo4j-backed implementation
# ---------------------------------------------------------------------------

class Neo4jBackend:
    def __init__(self, uri: str, password: str, repos_json: str):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=("neo4j", password))
        self.repo_roots: Dict[str, Path] = {}
        for repo in json.loads(Path(repos_json).read_text()):
            self.repo_roots[repo["name"]] = Path(repo["path"]).resolve()
        self.annotations_path = SCRIPT_DIR / "annotations.json"
        self._parser = None

    def close(self):
        self.driver.close()

    # -- annotations (mutating tools) --------------------------------------

    def _annotations(self) -> Dict[str, Any]:
        if self.annotations_path.exists():
            return json.loads(self.annotations_path.read_text())
        return {}

    def _save_annotations(self, data: Dict[str, Any]) -> None:
        self.annotations_path.write_text(json.dumps(data, indent=2) + "\n")

    def set_summary(self, program: str, summary: str) -> None:
        data = self._annotations()
        data.setdefault(program, {})["summary"] = summary
        self._save_annotations(data)

    def set_prd(self, program: str, documentation: str) -> None:
        data = self._annotations()
        data.setdefault(program, {})["prd"] = documentation
        self._save_annotations(data)

    def set_classification(self, program: str, classification: str) -> None:
        data = self._annotations()
        data.setdefault(program, {})["classification"] = classification
        self._save_annotations(data)

    # -- lookups ------------------------------------------------------------

    def _resolve_type(self, program: str) -> Optional[dict]:
        """Resolve a program by simple name or FQN (prefer declared types).

        multi-repo-graph fix: `repo` previously came from
        Repository→Package→Type, but Package nodes MERGE across repos in a
        portfolio (org.acme in repo A and repo B is ONE node), so a type
        could be attributed to a repo whose tree doesn't contain its file —
        source reads then failed (FileNotFoundError). repo now comes from the
        DEFINES File's repo-prefixed id, so file/sha256/repo are always the
        same declaration. java-graph never hit this: its two repos had
        disjoint package namespaces."""
        with self.driver.session() as s:
            rec = s.run("""
                MATCH (t:Type)
                WHERE t.name = $p OR t.fqn = $p
                OPTIONAL MATCH (f:File)-[:DEFINES]->(t)
                WITH t, f ORDER BY t.external ASC, t.fqn, f.id LIMIT 1
                RETURN t.fqn AS fqn, t.name AS name, t.kind AS kind,
                       f.path AS file, f.sha256 AS sha256,
                       (CASE WHEN f.id IS NULL THEN null
                             ELSE split(f.id, ':')[0] END) AS repo
            """, p=program).single()
            return dict(rec) if rec else None

    def search_types(self, name: str) -> List[dict]:
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (t:Type {external: false})
                WHERE t.name = $n OR t.fqn = $n OR t.name STARTS WITH $n
                RETURN t.name AS name, t.fqn AS fqn, t.kind AS kind,
                       [(f:File)-[:DEFINES]->(t) | split(f.id, ':')[0]][0] AS repo,
                       t.external = false AS ok
                ORDER BY t.fqn LIMIT 25
            """, n=name)]

    def search_types_prefix(self, prefix: str) -> List[dict]:
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (t:Type {external: false})
                WHERE t.name STARTS WITH $p
                RETURN t.name AS name, t.fqn AS fqn, t.kind AS kind,
                       [(f:File)-[:DEFINES]->(t) | split(f.id, ':')[0]][0] AS repo,
                       true AS ok
                ORDER BY t.fqn LIMIT 50
            """, p=prefix)]

    # -- Tier A -------------------------------------------------------------

    def source_state(self, program: str) -> Optional[dict]:
        """Resolve program -> {file, repo, lines, hashMatch} for drift-aware reads."""
        t = self._resolve_type(program)
        if not t or not t.get("file") or t.get("repo") not in self.repo_roots:
            return None
        text = (self.repo_roots[t["repo"]] / t["file"]).read_text(
            encoding="utf-8", errors="replace")
        return {"file": t["file"], "repo": t["repo"], "lines": text.splitlines(),
                "hashMatch": drift_check(t.get("sha256"), text)}

    def get_source(self, program: str, start: int, end: int) -> Optional[dict]:
        state = self.source_state(program)
        if state is None:
            return None
        return {"code": slice_lines(state["lines"], start, end),
                "file": state["file"], "hashMatch": state["hashMatch"]}

    def search_in_file(self, program: str, pattern: str, max_results: int = 100) -> Optional[dict]:
        state = self.source_state(program)
        if state is None:
            return None
        t = self._resolve_type(program)
        hits = text_search(state["lines"], pattern, max_results)
        for h in hits:
            h.update({"program": t["name"], "paragraphName": None, "deadCode": False})
        return {"hits": hits, "file": state["file"], "hashMatch": state["hashMatch"]}

    def _portfolio_file_list(self) -> List[tuple]:
        with self.driver.session() as s:
            return [(r["repo"], r["path"]) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File) RETURN r.name AS repo, f.path AS path
            """)]

    @staticmethod
    def _hit(repo: str, path: str, line_no: int, text: str) -> dict:
        return {"program": path.rsplit("/", 1)[-1].split(".")[0],
                "repo": repo, "file": path, "line": line_no, "text": text.rstrip()}

    def _rg_search(self, rg: str, pattern: str, files: List[tuple],
                   limit: int) -> Optional[List[dict]]:
        """ripgrep fast path; returns None if rg is unavailable or fails."""
        by_root: Dict[Path, List[tuple]] = {}
        for repo, path in files:
            root = self.repo_roots.get(repo)
            if root:
                by_root.setdefault(root.resolve(), []).append((repo, path))
        results: List[dict] = []
        counts: Dict[str, int] = {}
        for root, entries in by_root.items():
            known = {path for _, path in entries}
            try:
                proc = subprocess.run(
                    [rg, "-n", "-i", "-F", "--no-heading", "--glob", "*.java",
                     pattern, str(root)],
                    capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode not in (0, 1):  # 1 = no matches
                return None
            for row in proc.stdout.splitlines():
                abs_path, line_no, text = row.split(":", 2)
                try:
                    rel = str(Path(abs_path).resolve().relative_to(root))
                except ValueError:
                    continue
                if rel not in known:
                    continue
                repo = next(r for r, p in entries if p == rel)
                program = rel.rsplit("/", 1)[-1].split(".")[0]
                if counts.get(program, 0) >= limit:
                    continue
                counts[program] = counts.get(program, 0) + 1
                results.append(self._hit(repo, rel, int(line_no), text))
        return results

    def _parallel_search(self, pattern: str, files: List[tuple],
                         limit: int) -> List[dict]:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)

        def scan(entry):
            repo, path = entry
            root = self.repo_roots.get(repo, Path("/nonexistent"))
            out = []
            count = 0
            for idx, line in enumerate(read_lines(root, path), 1):
                if rx.search(line):
                    out.append(self._hit(repo, path, idx, line))
                    count += 1
                    if count >= limit:
                        break
            return out

        results: List[dict] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for file_hits in pool.map(scan, files):
                results.extend(file_hits)
        return results

    def search_portfolio_text(self, pattern: str, max_per_program: int = 0) -> List[dict]:
        limit = max_per_program if max_per_program > 0 else 10_000
        files = self._portfolio_file_list()
        rg = find_rg()
        if rg:
            hits = self._rg_search(rg, pattern, files, limit)
            if hits is not None:
                return hits
        return self._parallel_search(pattern, files, limit)

    def deep_call_chain(self, program: str, max_depth: int = 10) -> Optional[dict]:
        t = self._resolve_type(program)
        if not t:
            return None

        def methods_of_type(fqn: str) -> List[dict]:
            with self.driver.session() as s:
                return [dict(r) for r in s.run("""
                    MATCH (:Type {fqn: $f})-[:CONTAINS]->(m:Method)
                    RETURN m.id AS id, m.name AS name
                """, f=fqn)]

        def calls_of_method(mid: str) -> List[dict]:
            with self.driver.session() as s:
                return [dict(r) for r in s.run("""
                    MATCH (m:Method {id: $i})-[c:CALLS]->(ct:CallTarget)
                    RETURN ct.name AS callee, c.count AS count ORDER BY count DESC
                """, i=mid)]

        def resolve_callee(callee: str, package: str) -> Optional[str]:
            with self.driver.session() as s:
                rec = s.run("""
                    MATCH (m:Method {name: $c})<-[:CONTAINS]-(t:Type)
                    RETURN m.id AS id,
                           t.fqn STARTS WITH $pkg AS same_pkg
                    ORDER BY same_pkg DESC, m.id LIMIT 1
                """, c=callee, pkg=package).single()
                return rec["id"] if rec else None

        package = t["fqn"].rsplit(".", 1)[0] if "." in t["fqn"] else ""
        visited = set()

        def expand(fqn: str, depth: int) -> List[dict]:
            if depth > max_depth:
                return []
            out = []
            for m in methods_of_type(fqn):
                node = {"name": m["name"], "nodeType": "method", "children": []}
                for call in calls_of_method(m["id"])[:20]:
                    resolved = resolve_callee(call["callee"], package)
                    if resolved and resolved not in visited:
                        visited.add(resolved)
                        child_fqn = resolved.split("#")[0]
                        node["children"].append({
                            "name": call["callee"],
                            "nodeType": "method",
                            "resolvedTo": resolved,
                            "children": expand(child_fqn, depth + 1),
                        })
                    else:
                        node["children"].append({"name": call["callee"], "children": []})
                out.append(node)
            return out

        return {
            "program": t["name"],
            "fqn": t["fqn"],
            "paths": [{"name": t["name"], "nodeType": "entry_point",
                       "children": expand(t["fqn"], 1)}],
        }

    # -- Tier B -------------------------------------------------------------

    def overview(self) -> dict:
        with self.driver.session() as s:
            def one(q, **p):
                return s.run(q, **p).single()["c"]
            return {
                "repositories": one("MATCH (r:Repository) RETURN count(r) AS c"),
                "files": one("MATCH (:Repository)-[:CONTAINS]->(f:File) RETURN count(f) AS c"),
                "programs": one("""MATCH (:Repository)-[:CONTAINS]->(:File)-[:DEFINES]->(t:Type)
                                   RETURN count(DISTINCT t) AS c"""),
                "packages": one("MATCH (:Repository)-[:CONTAINS]->(p:Package) RETURN count(p) AS c"),
                "methods": one("""MATCH (:Repository)-[:CONTAINS]->(:File)-[:DEFINES]->(:Type)-[:CONTAINS]->(m:Method)
                                  RETURN count(DISTINCT m) AS c"""),
                "fields": one("""MATCH (:Repository)-[:CONTAINS]->(:File)-[:DEFINES]->(:Type)-[:CONTAINS]->(fl:Field)
                                 RETURN count(DISTINCT fl) AS c"""),
                "parserErrors": 0,
                "deadCodeSegments": 0,
                "totalDependencies": one(
                    "MATCH (:File)-[:IMPORTS]->() RETURN count(*) AS c"),
            }

    def portfolio_files(self) -> List[dict]:
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                OPTIONAL MATCH (f)-[:DEFINES]->(t:Type)
                RETURN r.name AS repo, f.path AS file,
                       count(t) AS definedTypes ORDER BY repo, file
            """)]

    def assets(self) -> dict:
        with self.driver.session() as s:
            def one(q):
                return s.run(q).single()["c"]
            return {
                "programs": one("""MATCH (:Repository)-[:CONTAINS]->(:File)-[:DEFINES]->(t:Type)
                                   RETURN count(DISTINCT t) AS c"""),
                "packages": one("MATCH (:Repository)-[:CONTAINS]->(p:Package) RETURN count(p) AS c"),
                "jclJobs": 0,
                "copybooks": 0,
            }

    def complex_programs(self) -> List[dict]:
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (t:Type {external:false})-[:CONTAINS]->(m:Method)
                OPTIONAL MATCH (m)-[c:CALLS]->()
                WITH t, count(m) AS methods, sum(coalesce(c.count, 0)) AS calls
                RETURN t.name AS program, t.kind AS kind, t.fqn AS fqn,
                       methods + calls AS complexity, methods, calls
                ORDER BY complexity DESC LIMIT 50
            """)]

    def export(self, program: str) -> Optional[dict]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            methods = [dict(r) for r in s.run("""
                MATCH (:Type {fqn: $f})-[:CONTAINS]->(m:Method)
                RETURN m.id AS id, m.name AS name, m.arity AS arity,
                       m.return_type AS returnType, m.modifiers AS modifiers,
                       m.line AS line
                ORDER BY m.line
            """, f=t["fqn"])]
            fields = [dict(r) for r in s.run("""
                MATCH (:Type {fqn: $f})-[:CONTAINS]->(fl:Field)
                RETURN fl.name AS name, fl.type AS type, fl.modifiers AS modifiers,
                       fl.line AS line ORDER BY fl.line
            """, f=t["fqn"])]
            bases = [dict(r) for r in s.run("""
                MATCH (t:Type {fqn: $f})-[r:EXTENDS|IMPLEMENTS]->(b)
                RETURN type(r) AS rel, b.fqn AS target
            """, f=t["fqn"])]
            imports = [dict(r) for r in s.run("""
                MATCH (f:File)-[:DEFINES]->(:Type {fqn: $f2})
                MATCH (f)-[:IMPORTS]->(i)
                RETURN i.fqn AS target LIMIT 100
            """, f2=t["fqn"])]
        return {"program": t["name"], "fqn": t["fqn"], "kind": t["kind"],
                "file": t.get("file"), "repo": t.get("repo"),
                "methods": methods, "fields": fields, "bases": bases,
                "imports": imports}

    def paragraphs(self, program: str) -> Optional[List[dict]]:
        """Methods with start/end lines, computed by re-parsing the source."""
        t = self._resolve_type(program)
        if not t or not t.get("file"):
            return None
        state = self.source_state(program)
        if state is None:
            return None
        return method_spans(state["lines"], t["name"])

    def paragraph_details(self, program: str, name: str) -> Optional[dict]:
        spans = self.paragraphs(program)
        if spans is None:
            return None
        for m in spans:
            if m["name"].lower() == name.lower():
                state = self.source_state(program)
                return {"name": m["name"], "startLine": m["startLine"],
                        "endLine": m["endLine"],
                        "code": slice_lines(state["lines"], m["startLine"], m["endLine"]),
                        "file": state["file"], "hashMatch": state["hashMatch"]}
        return None

    def control_flow(self, program: str, package: Optional[str] = None) -> Optional[dict]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            internal = [dict(r) for r in s.run("""
                MATCH (t:Type {fqn: $f})-[:CONTAINS]->(m:Method)
                MATCH (m)-[:CALLS]->(ct:CallTarget)<-[:CONTAINS]-(m2:Method)<-[:CONTAINS]-(t2)
                WHERE t2.fqn = $f
                RETURN DISTINCT m.name AS from, m2.name AS to
            """, f=t["fqn"])]
            methods = [dict(r) for r in s.run("""
                MATCH (t:Type {fqn: $f})-[:CONTAINS]->(m:Method) RETURN m.name AS name
            """, f=t["fqn"])]
        return {
            "program": t["name"], "packageName": package or "",
            "nodes": [{"id": m["name"], "type": "method"} for m in methods],
            "edges": [{"from": e["from"], "to": e["to"], "type": "CALL"}
                      for e in internal],
        }

    def call_chain_page(self, page: int, page_size: int) -> dict:
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (t1:Type {external:false})-[:CONTAINS]->(:Method)-[:CALLS]->
                      (ct:CallTarget)<-[:CONTAINS]-(:Method)<-[:CONTAINS]-(t2:Type)
                WHERE t1 <> t2
                RETURN DISTINCT t1.name AS caller, t2.name AS callee, ct.name AS via
                ORDER BY caller, callee
            """)]
        known = {r["caller"] for r in rows} | {r["callee"] for r in rows}
        with self.driver.session() as s:
            all_types = [r["name"] for r in s.run(
                "MATCH (t:Type {external:false}) RETURN t.name AS name")]
        nodes = []
        for name in all_types:
            if name in known:
                ntype = "entry_point"
            elif any(r["callee"] == name for r in rows):
                ntype = "external_program"
            else:
                ntype = "isolated_program"
            nodes.append({"name": name, "type": ntype})
        start = page * page_size
        chunk = rows[start:start + page_size]
        return {
            "nodes": nodes,
            "edges": [{"from": e["caller"], "to": e["callee"], "callSite": e["via"]}
                      for e in chunk],
            "page": page, "pageSize": page_size,
            "hasNext": start + page_size < len(rows),
            "totalEdges": len(rows),
        }

    def find_call_paths(self, from_program: str, to_program: str,
                        min_depth: int, max_depth: int) -> List[List[str]]:
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (t1:Type {external:false})-[:CONTAINS]->(:Method)-[:CALLS]->
                      (ct:CallTarget)<-[:CONTAINS]-(:Method)<-[:CONTAINS]-(t2:Type)
                WHERE t1 <> t2
                RETURN DISTINCT t1.name AS caller, t2.name AS callee
            """)]
        graph: Dict[str, List[str]] = {}
        for r in rows:
            graph.setdefault(r["caller"], []).append(r["callee"])
        paths: List[List[str]] = []

        def dfs(node: str, path: List[str]):
            if len(path) - 1 > max_depth:
                return
            if to_program and node == to_program and len(path) - 1 >= min_depth:
                paths.append(list(path))
                return
            for nxt in graph.get(node, []):
                if nxt not in path:
                    dfs(nxt, path + [nxt])

        dfs(from_program, [from_program])
        return paths[:25]

    def ibmi_usage(self, program: str) -> Optional[List[dict]]:
        """Referenced types/packages (imports) with resolution state."""
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (f:File)-[:DEFINES]->(:Type {fqn: $f})
                MATCH (f)-[:IMPORTS]->(i)
                RETURN i.fqn AS name, 'IMPORT' AS accessMode,
                       EXISTS { MATCH (:Type {fqn: i.fqn, external: false}) } AS resolved
                ORDER BY name LIMIT 200
            """, f=t["fqn"])]

    def ibmi_usage_portfolio(self) -> dict:
        rows = self.portfolio_files()
        by_repo: Dict[str, int] = {}
        for r in rows:
            by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
        return {"fileCount": sum(by_repo.values()), "byRepository": by_repo}

    def lineage_sources(self, program: str, include_internal: bool) -> Optional[List[dict]]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (t:Type {fqn: $f})-[r:EXTENDS|IMPLEMENTS]->(src)
                RETURN DISTINCT type(r) AS relation, src.fqn AS source,
                       src.external = false AS internal
                ORDER BY source
            """, f=t["fqn"])]

    def lineage_targets(self, program: str, include_internal: bool) -> Optional[List[dict]]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            derived = [dict(r) for r in s.run("""
                MATCH (d:Type {external:false})-[:EXTENDS|IMPLEMENTS]->(t:Type {fqn: $f})
                RETURN 'DERIVED' AS relation, d.fqn AS target, true AS internal
                ORDER BY target
            """, f=t["fqn"])]
            callers = [dict(r) for r in s.run("""
                MATCH (caller:Type {external:false})-[:CONTAINS]->(:Method)-[:CALLS]->
                      (ct:CallTarget)<-[:CONTAINS]-(m:Method)<-[:CONTAINS]-(t:Type {fqn: $f})
                RETURN DISTINCT 'CALLED_BY' AS relation, caller.fqn AS target, true AS internal
                LIMIT 50
            """, f=t["fqn"])]
        out = derived + callers
        return out if include_internal else [r for r in out if not r["internal"]] or out[:5]

    def trace_lineage(self, target: str, direction: str, max_depth: int) -> dict:
        edges: List[dict] = []

        def expand(fqn: str, depth: int):
            if depth >= max_depth:
                return
            with self.driver.session() as s:
                rel = "EXTENDS|IMPLEMENTS"
                q = (f"MATCH (a:Type {{fqn: $f}})-[:{rel}]->(b) RETURN a.fqn AS a, b.fqn AS b"
                     if direction == "forward"
                     else f"MATCH (b)-[:{rel}]->(a:Type {{fqn: $f}}) RETURN a.fqn AS a, b.fqn AS b")
                for r in s.run(q, f=fqn):
                    edge = {"from": r["a"], "to": r["b"], "depth": depth + 1}
                    if edge not in edges:
                        edges.append(edge)
                    expand(r["b"] if direction == "forward" else r["a"], depth + 1)

        expand(target, 0)
        return {"target": target, "direction": direction, "edges": edges,
                "certainty": "DEFINITE" if edges else "NONE"}

    def data_structures(self, program: str) -> Optional[List[dict]]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (:Type {fqn: $f})-[:CONTAINS]->(fl:Field)
                RETURN fl.name AS name, fl.type AS declaredType, fl.line AS line,
                       fl.modifiers AS modifiers ORDER BY fl.line
            """, f=t["fqn"])]

    def data_structure_details(self, program: str, name: str) -> Optional[dict]:
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            rec = s.run("""
                MATCH (:Type {fqn: $f})-[:CONTAINS]->(fl:Field {name: $n})
                RETURN fl.name AS name, fl.type AS declaredType, fl.line AS line,
                       fl.modifiers AS modifiers
            """, f=t["fqn"], n=name).single()
            return dict(rec) if rec else None

    # -- Tier C -------------------------------------------------------------

    def dead_paragraphs(self, program: str) -> Optional[List[dict]]:
        """Methods never cited as a call target inside their own repository."""
        t = self._resolve_type(program)
        if not t:
            return None
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (t:Type {fqn: $f})-[:CONTAINS]->(m:Method)
                WHERE NOT EXISTS {
                    MATCH (repo:Repository)-[:CONTAINS]->(:Package)-[:CONTAINS]->(t2)
                    MATCH (t2)-[:CONTAINS]->(:Method)-[:CALLS]->(:CallTarget {name: m.name})
                    WHERE repo.name = $repo
                }
                  AND NOT m.name IN ['main', 'toString', 'hashCode', 'equals',
                                     'close', 'run', 'call', 'apply', 'accept',
                                     'get', 'set', 'process', 'execute', 'handle']
                RETURN m.name AS name, m.line AS line ORDER BY m.line LIMIT 50
            """, f=t["fqn"], repo=t.get("repo") or "")]

    def call_chain_statistics(self) -> dict:
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (t1:Type {external:false})-[:CONTAINS]->(:Method)-[:CALLS]->
                      (ct:CallTarget)<-[:CONTAINS]-(:Method)<-[:CONTAINS]-(t2:Type)
                WHERE t1 <> t2
                RETURN DISTINCT t1.name AS caller, t2.name AS callee, ct.name AS via
            """)]
        from collections import Counter
        called = Counter(r["callee"] for r in rows)
        return {
            "totalEdges": len(rows),
            "mostCalledModules": [{"name": n, "callCount": c}
                                  for n, c in called.most_common(10)],
        }

    def migration_impact(self, program: str) -> Optional[dict]:
        t = self._resolve_type(program)
        if not t:
            return None
        imports = self.ibmi_usage(program) or []
        callers = self.lineage_targets(program, True) or []
        ext = [i for i in imports if not i["resolved"]]
        risk = "HIGH" if len(ext) > 20 else ("MEDIUM" if ext else "LOW")
        return {
            "program": t["name"], "fqn": t["fqn"],
            "fileCount": len(imports), "unresolvedDependencies": len(ext),
            "tables": [], "callers": len(callers), "risk": risk,
        }

    def modernization_start(self, limit: int) -> List[dict]:
        programs = []
        with self.driver.session() as s:
            for r in s.run("""
                MATCH (t:Type {external:false})-[:CONTAINS]->(m:Method)
                OPTIONAL MATCH (t)<-[:EXTENDS|IMPLEMENTS]-()
                WITH t, count(DISTINCT m) AS methods, count{(t)<-[:EXTENDS|IMPLEMENTS]-()} AS derived
                WHERE methods > 0
                RETURN t.name AS program, t.fqn AS fqn, methods, derived,
                       methods + derived * 2 AS score
                ORDER BY score ASC, fqn LIMIT $limit
            """, limit=limit):
                programs.append(dict(r))
        return programs

    def auto_summary(self, program: str) -> Optional[str]:
        exp = self.export(program)
        if not exp:
            return None
        bases = ", ".join(b["target"] for b in exp["bases"]) or "none"
        return (f"{exp['kind']} {exp['program']} ({exp['fqn']}) in {exp['repo']}: "
                f"{len(exp['methods'])} methods, {len(exp['fields'])} fields; "
                f"bases: {bases}.")

    def get_annotation(self, program: str, key: str) -> Optional[str]:
        return (self._annotations().get(program) or {}).get(key)


# ---------------------------------------------------------------------------
# Method span extraction via Tree-sitter (shared by both backends)
# ---------------------------------------------------------------------------

def method_spans(lines: List[str], type_name: str) -> List[dict]:
    import tree_sitter
    import tree_sitter_java
    language = tree_sitter.Language(tree_sitter_java.language())
    try:
        parser = tree_sitter.Parser(language)
    except TypeError:
        parser = tree_sitter.Parser()
        parser.set_language(language)
    root = parser.parse("\n".join(lines).encode()).root_node

    spans: List[dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                spans.append({
                    "name": name_node.text.decode(),
                    "startLine": node.start_point[0] + 1,
                    "endLine": node.end_point[0] + 1,
                })
        for child in node.children:
            stack.append(child)
    spans.sort(key=lambda s: s["startLine"])
    return spans


# ---------------------------------------------------------------------------
# Fake backend (offline tests)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Inventory statistics (design §6 extension tools)
# ---------------------------------------------------------------------------

# ---- Neo4jBackend methods (appended to class via monkey-free mixin below) ----


def _neo4j_stats_methods(cls):
    """Attach the inventory-statistics methods to Neo4jBackend."""

    def file_statistics(self, repository: Optional[str] = None):
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (r:Repository)
                WHERE $repo IS NULL OR r.name = $repo
                OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
                RETURN r.name AS repository,
                       count(f) AS filesInGraph,
                       sum(CASE WHEN exists((f)-[:DEFINES]->()) THEN 1 ELSE 0 END) AS deep,
                       sum(CASE WHEN f.kind = 'artifact' OR f.category IN ['image','font','binary_blob']
                                THEN 1 ELSE 0 END) AS binaries,
                       [(sp:StatsSnapshot)-[:COVERS]->(r) | sp.runId][0] AS runId
                ORDER BY repository
            """, repo=repository)]
            for row in rows:
                snap = None
                rec = s.run("""
                    MATCH (sp:StatsSnapshot)-[c:COVERS]->(:Repository {name: $n})
                    WITH sp, c ORDER BY sp.at DESC LIMIT 1
                    RETURN c {filesTracked: c.filesTracked, filesOnDisk: c.filesOnDisk,
                              untrackedFiles: c.untrackedFiles,
                              filesIndexedDeep: c.filesIndexedDeep,
                              filesInventoryOnly: c.filesInventoryOnly,
                              binaries: c.binaries} AS cov
                """, n=row["repository"]).single()
                if rec:
                    snap = rec["cov"]
                row["coverage"] = snap
        return {"repositories": rows}

    def file_categories(self, repository: Optional[str] = None,
                        path_prefix: Optional[str] = None,
                        kind: Optional[str] = None):
        with self.driver.session() as s:
            return [dict(r) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                WHERE ($repo IS NULL OR r.name = $repo)
                  AND ($prefix IS NULL OR f.path STARTS WITH $prefix)
                  AND ($kind IS NULL OR f.kind = $kind)
                RETURN f.kind AS kind, f.category AS category,
                       count(f) AS files, sum(f.bytes) AS bytes
                ORDER BY files DESC
            """, repo=repository, prefix=path_prefix, kind=kind)]

    def loc_statistics(self, repository: Optional[str] = None,
                       category: Optional[str] = None,
                       path_prefix: Optional[str] = None):
        with self.driver.session() as s:
            rows = [dict(r) for r in s.run("""
                MATCH (r:Repository)-[:CONTAINS]->(f:File)
                WHERE ($repo IS NULL OR r.name = $repo)
                  AND ($cat IS NULL OR f.category = $cat)
                  AND ($prefix IS NULL OR f.path STARTS WITH $prefix)
                RETURN r.name AS repository, f.category AS category, count(f) AS files,
                       sum(f.totalLines) AS totalLines, sum(f.codeLines) AS codeLines,
                       sum(f.commentLines) AS commentLines,
                       sum(f.blankLines) AS blankLines
                ORDER BY totalLines DESC
            """, repo=repository, cat=category, prefix=path_prefix)]
        totals = {k: sum(r[k] or 0 for r in rows)
                  for k in ("files", "totalLines", "codeLines",
                            "commentLines", "blankLines")}
        return {"rows": rows, "totals": totals}

    def file_details(self, path: str, repository: str):
        with self.driver.session() as s:
            rec = s.run(
                "MATCH (f:File {id: $id}) RETURN properties(f) AS props",
                id=f"{repository}:{path}").single()
        if rec is None:
            return None
        props = rec["props"]
        root = self.repo_roots.get(repository)
        hash_match = None
        if root is not None and props.get("sha256"):
            try:
                text = (root / path).read_text(encoding="utf-8", errors="replace")
                hash_match = props["sha256"] == sha256_text(text)
            except OSError:
                hash_match = None
        props["repository"] = repository
        props["fileHashMatch"] = hash_match
        props["deepIndexed"] = props.get("extension") == "java"
        return props

    cls.file_statistics = file_statistics
    cls.file_categories = file_categories
    cls.loc_statistics = loc_statistics
    cls.file_details = file_details
    return cls


_neo4j_stats_methods(Neo4jBackend)


ORDER_SERVICE = """package org.demo;

import java.util.List;
import org.demo.OrderRepository;

public class OrderService extends AbstractService implements AutoCloseable {
    private final OrderRepository repo;
    protected int counter;

    public void process(String id) {
        validate(id);
        repo.save(id);
        log.info("processed");
    }

    private void validate(String id) {
        if (id == null) { throw new IllegalArgumentException("id"); }
    }

    @Override
    public void close() { cleanup(); }

    void cleanup() { }
}
"""

ORDER_REPOSITORY = """package org.demo;

public interface OrderRepository {
    void save(String id);
    String find(String id);
}
"""

ABSTRACT_SERVICE = """package org.demo;

public class AbstractService {
    protected final org.demo.Logger log = new org.demo.Logger();
}
"""


class FakeBackend:
    """In-memory portfolio mirroring the graph model, with sources on disk."""

    def __init__(self, source_root: Path):
        self.root = source_root
        src = source_root / "src" / "org" / "demo"
        src.mkdir(parents=True, exist_ok=True)
        (src / "OrderService.java").write_text(ORDER_SERVICE)
        (src / "OrderRepository.java").write_text(ORDER_REPOSITORY)
        (src / "AbstractService.java").write_text(ABSTRACT_SERVICE)
        self.annotations: Dict[str, Dict[str, str]] = {}

        self.types = {
            "org.demo.OrderService": {
                "name": "OrderService", "kind": "class", "repo": "demo",
                "file": "src/org/demo/OrderService.java",
                "methods": [
                    {"name": "process", "line": 10, "calls": ["validate", "save", "info"]},
                    {"name": "validate", "line": 16, "calls": []},
                    {"name": "close", "line": 21, "calls": ["cleanup"]},
                    {"name": "cleanup", "line": 23, "calls": []},
                ],
                "fields": [
                    {"name": "repo", "type": "OrderRepository", "line": 7,
                     "modifiers": "private final"},
                    {"name": "counter", "type": "int", "line": 8, "modifiers": "protected"},
                ],
                "extends": ["org.demo.AbstractService"],
                "implements": ["java.lang.AutoCloseable"],
                "imports": ["java.util.List", "org.demo.OrderRepository"],
                "sha256": sha256_text(ORDER_SERVICE),
            },
            "org.demo.OrderRepository": {
                "name": "OrderRepository", "kind": "interface", "repo": "demo",
                "file": "src/org/demo/OrderRepository.java",
                "methods": [
                    {"name": "save", "line": 4, "calls": []},
                    {"name": "find", "line": 5, "calls": []},
                ],
                "fields": [], "extends": [], "implements": [],
                "imports": [],
                "sha256": sha256_text(ORDER_REPOSITORY),
            },
            "org.demo.AbstractService": {
                "name": "AbstractService", "kind": "class", "repo": "demo",
                "file": "src/org/demo/AbstractService.java",
                "methods": [], "fields": [], "extends": [], "implements": [],
                "imports": [],
                "sha256": sha256_text(ABSTRACT_SERVICE),
            },
        }

    # -- inventory statistics (extension tools) -----------------------------

    def _inventory_records(self):
        return [
            {"path": "src/org/demo/OrderService.java", "kind": "source",
             "category": "java_backend", "role": "main", "extension": "java",
             "bytes": 500, "totalLines": 25, "codeLines": 20,
             "commentLines": 2, "blankLines": 3, "sha256": None},
            {"path": "src/org/demo/OrderRepository.java", "kind": "source",
             "category": "java_backend", "role": "main", "extension": "java",
             "bytes": 130, "totalLines": 7, "codeLines": 5,
             "commentLines": 0, "blankLines": 2, "sha256": None},
            {"path": "src/org/demo/AbstractService.java", "kind": "source",
             "category": "java_backend", "role": "main", "extension": "java",
             "bytes": 110, "totalLines": 4, "codeLines": 4,
             "commentLines": 0, "blankLines": 0, "sha256": None},
            {"path": "src/org/demo/OrderServiceTest.java", "kind": "source",
             "category": "java_test", "role": "test", "extension": "java",
             "bytes": 80, "totalLines": 5, "codeLines": 4,
             "commentLines": 1, "blankLines": 0, "sha256": None},
            {"path": "ui/app.component.ts", "kind": "source",
             "category": "frontend_logic", "role": "main", "extension": "ts",
             "bytes": 300, "totalLines": 18, "codeLines": 15,
             "commentLines": 1, "blankLines": 2, "sha256": None},
            {"path": "ui/theme.scss", "kind": "source",
             "category": "frontend_style", "role": "main", "extension": "scss",
             "bytes": 90, "totalLines": 8, "codeLines": 8,
             "commentLines": 0, "blankLines": 0, "sha256": None},
            {"path": "pom.xml", "kind": "build", "category": "build_script",
             "role": "main", "extension": "xml", "bytes": 700,
             "totalLines": 30, "codeLines": 28, "commentLines": 0,
             "blankLines": 2, "sha256": None},
            {"path": "README.md", "kind": "docs", "category": "documentation",
             "role": "main", "extension": "md", "bytes": 45,
             "totalLines": 3, "codeLines": 3, "commentLines": 0,
             "blankLines": 0, "sha256": None},
            {"path": "logo.png", "kind": "assets", "category": "image",
             "role": "main", "extension": "png", "bytes": 2048,
             "totalLines": None, "codeLines": None, "commentLines": None,
             "blankLines": None, "sha256": None},
        ]

    def file_statistics(self, repository=None):
        recs = self._inventory_records()
        deep = sum(1 for r in recs if r["extension"] == "java")
        binaries = sum(1 for r in recs if r["kind"] in ("assets", "artifact"))
        return {"repositories": [{
            "repository": repository or "demo",
            "filesInGraph": len(recs), "deep": deep,
            "binaries": binaries, "runId": None,
            "coverage": {"filesTracked": len(recs), "filesOnDisk": len(recs),
                         "untrackedFiles": 0, "filesIndexedDeep": deep,
                         "filesInventoryOnly": len(recs) - deep - binaries + binaries,
                         "binaries": binaries},
        }]}

    def file_categories(self, repository=None, path_prefix=None, kind=None):
        out = {}
        for r in self._inventory_records():
            if path_prefix and not r["path"].startswith(path_prefix):
                continue
            if kind and r["kind"] != kind:
                continue
            agg = out.setdefault((r["kind"], r["category"]),
                                 {"kind": r["kind"], "category": r["category"],
                                  "files": 0, "bytes": 0})
            agg["files"] += 1
            agg["bytes"] += r["bytes"]
        return sorted(out.values(), key=lambda x: -x["files"])

    def loc_statistics(self, repository=None, category=None, path_prefix=None):
        rows = []
        for r in self._inventory_records():
            if path_prefix and not r["path"].startswith(path_prefix):
                continue
            if category and r["category"] != category:
                continue
            rows.append({"repository": repository or "demo",
                         "category": r["category"], "files": 1,
                         "totalLines": r["totalLines"] or 0,
                         "codeLines": r["codeLines"] or 0,
                         "commentLines": r["commentLines"] or 0,
                         "blankLines": r["blankLines"] or 0})
        agg = {}
        for r in rows:
            if r["category"] in agg:
                a = agg[r["category"]]
                for k in ("files", "totalLines", "codeLines", "commentLines", "blankLines"):
                    a[k] += r[k]
            else:
                agg[r["category"]] = dict(r)
        out = sorted(agg.values(), key=lambda x: -x["totalLines"])
        totals = {k: sum(r[k] or 0 for r in out)
                  for k in ("files", "totalLines", "codeLines",
                            "commentLines", "blankLines")}
        return {"rows": out, "totals": totals}

    def file_details(self, path, repository):
        for r in self._inventory_records():
            if r["path"] == path:
                d = dict(r)
                d["repository"] = repository
                d["fileHashMatch"] = None
                d["deepIndexed"] = r["extension"] == "java"
                return d
        return None

    # -- annotations --------------------------------------------------------

    def set_summary(self, program, summary): self.annotations.setdefault(program, {})["summary"] = summary
    def set_prd(self, program, documentation): self.annotations.setdefault(program, {})["prd"] = documentation
    def set_classification(self, program, classification): self.annotations.setdefault(program, {})["classification"] = classification
    def get_annotation(self, program, key):
        return (self.annotations.get(program) or {}).get(key)

    # -- lookups ------------------------------------------------------------

    def _resolve_type(self, program):
        for fqn, t in self.types.items():
            if t["name"] == program or fqn == program:
                return {"fqn": fqn, **t}
        return None

    def search_types(self, name):
        return [{"name": t["name"], "fqn": fqn, "kind": t["kind"], "repo": t["repo"],
                 "ok": True}
                for fqn, t in self.types.items()
                if t["name"].startswith(name) or fqn == name or fqn.startswith(name)]

    def search_types_prefix(self, prefix):
        return self.search_types(prefix)

    # -- Tier A -------------------------------------------------------------

    def source_state(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        text = (self.root / t["file"]).read_text(encoding="utf-8", errors="replace")
        return {"file": t["file"], "repo": t["repo"], "lines": text.splitlines(),
                "hashMatch": drift_check(t.get("sha256"), text)}

    def get_source(self, program, start, end):
        state = self.source_state(program)
        if state is None:
            return None
        return {"code": slice_lines(state["lines"], start, end),
                "file": state["file"], "hashMatch": state["hashMatch"]}

    def search_in_file(self, program, pattern, max_results=100):
        state = self.source_state(program)
        if state is None:
            return None
        t = self._resolve_type(program)
        hits = text_search(state["lines"], pattern, max_results)
        for h in hits:
            h.update({"program": t["name"], "paragraphName": None, "deadCode": False})
        return {"hits": hits, "file": state["file"], "hashMatch": state["hashMatch"]}

    def search_portfolio_text(self, pattern, max_per_program=0):
        out = []
        for fqn, t in self.types.items():
            hits = text_search(read_lines(self.root, t["file"]), pattern,
                               max_per_program or 100)
            for h in hits:
                h["program"] = t["name"]
                h["repo"] = t["repo"]
                h["file"] = t["file"]
            out.extend(hits)
        return out

    def deep_call_chain(self, program, max_depth=10):
        t = self._resolve_type(program)
        if not t:
            return None
        method_by_name = {m["name"]: m for m in t["methods"]}
        # fake cross-type resolution: save -> OrderRepository.save
        cross = {"save": ("OrderRepository", "save"), "find": ("OrderRepository", "find")}

        def expand(type_dict, depth):
            nodes = []
            for m in type_dict["methods"]:
                node = {"name": m["name"], "nodeType": "method", "children": []}
                for callee in m["calls"]:
                    if callee in method_by_name and depth < max_depth:
                        node["children"].append(
                            {"name": callee, "nodeType": "method", "children": []})
                    elif callee in cross and depth < max_depth:
                        other = self._resolve_type(cross[callee][0])
                        node["children"].append({
                            "name": callee, "nodeType": "method",
                            "children": expand(other, depth + 1)})
                    else:
                        node["children"].append({"name": callee, "children": []})
                nodes.append(node)
            return nodes

        return {"program": t["name"], "fqn": t["fqn"],
                "paths": [{"name": t["name"], "nodeType": "entry_point",
                           "children": expand(t, 1)}]}

    # -- Tier B -------------------------------------------------------------

    def overview(self):
        return {"repositories": 1, "files": 3, "programs": 3, "packages": 1,
                "methods": 6, "fields": 2, "parserErrors": 0,
                "deadCodeSegments": 0, "totalDependencies": 2}

    def portfolio_files(self):
        return [{"repo": t["repo"], "file": t["file"],
                 "definedTypes": 1 + len(
                     [n for n in t.get("nested", [])])}
                for t in self.types.values()]

    def assets(self):
        return {"programs": 3, "packages": 1, "jclJobs": 0, "copybooks": 0}

    def complex_programs(self):
        rows = []
        for fqn, t in self.types.items():
            calls = sum(len(m["calls"]) for m in t["methods"])
            rows.append({"program": t["name"], "kind": t["kind"], "fqn": fqn,
                         "complexity": len(t["methods"]) + calls,
                         "methods": len(t["methods"]), "calls": calls})
        return sorted(rows, key=lambda r: -r["complexity"])

    def export(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        return {
            "program": t["name"], "fqn": t["fqn"], "kind": t["kind"],
            "file": t["file"], "repo": t["repo"],
            "methods": [{"id": f"{t['fqn']}#{m['name']}/0", "name": m["name"],
                         "line": m["line"]} for m in t["methods"]],
            "fields": t["fields"],
            "bases": ([{"rel": "EXTENDS", "target": b} for b in t["extends"]] +
                      [{"rel": "IMPLEMENTS", "target": i} for i in t["implements"]]),
            "imports": [{"target": i} for i in t["imports"]],
        }

    def paragraphs(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        return method_spans(read_lines(self.root, t["file"]), t["name"])

    def paragraph_details(self, program, name):
        spans = self.paragraphs(program)
        if not spans:
            return None
        for m in spans:
            if m["name"].lower() == name.lower():
                state = self.source_state(program)
                return {"name": m["name"], "startLine": m["startLine"],
                        "endLine": m["endLine"],
                        "code": slice_lines(state["lines"], m["startLine"], m["endLine"]),
                        "file": state["file"], "hashMatch": state["hashMatch"]}
        return None

    def control_flow(self, program, package=None):
        t = self._resolve_type(program)
        if not t:
            return None
        own = {m["name"] for m in t["methods"]}
        edges = [{"from": m["name"], "to": c, "type": "CALL"}
                 for m in t["methods"] for c in m["calls"] if c in own]
        return {"program": t["name"], "packageName": package or "",
                "nodes": [{"id": m["name"], "type": "method"}
                          for m in t["methods"]],
                "edges": edges}

    def call_chain_page(self, page, page_size):
        rows = [
            {"from": "OrderService", "to": "OrderRepository", "callSite": "save"},
        ]
        nodes = [
            {"name": "OrderService", "type": "entry_point"},
            {"name": "AbstractService", "type": "isolated_program"},
            {"name": "OrderRepository", "type": "external_program"},
        ]
        start = page * page_size
        return {"nodes": nodes,
                "edges": rows[start:start + page_size],
                "page": page, "pageSize": page_size,
                "hasNext": False, "totalEdges": len(rows)}

    def find_call_paths(self, from_program, to_program, min_depth, max_depth):
        if (from_program, to_program) == ("OrderService", "OrderRepository") \
                and min_depth <= 1 <= max_depth:
            return [["OrderService", "OrderRepository"]]
        return []

    def ibmi_usage(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        return [{"name": i, "accessMode": "IMPORT",
                 "resolved": any(i == f for f in self.types)}
                for i in t["imports"]]

    def ibmi_usage_portfolio(self):
        return {"fileCount": 3, "byRepository": {"demo": 3}}

    def lineage_sources(self, program, include_internal):
        t = self._resolve_type(program)
        if not t:
            return None
        return ([{"relation": "EXTENDS", "source": b, "internal": b in self.types}
                 for b in t["extends"]] +
                [{"relation": "IMPLEMENTS", "source": i, "internal": i in self.types}
                 for i in t["implements"]])

    def lineage_targets(self, program, include_internal):
        t = self._resolve_type(program)
        if not t:
            return None
        out = []
        for fqn, other in self.types.items():
            if t["fqn"] in other["extends"] or t["fqn"] in other["implements"]:
                out.append({"relation": "DERIVED", "target": fqn, "internal": True})
        return out

    def trace_lineage(self, target, direction, max_depth):
        edges = []
        for fqn, t in self.types.items():
            for b in t["extends"]:
                edges.append({"from": fqn, "to": b, "depth": 1})
        # edges are {from: derived, to: base}: backward = ancestors (target's
        # own extends edges), forward = descendants (edges arriving at target)
        return {"target": target, "direction": direction,
                "edges": [e for e in edges
                          if (direction == "backward" and e["from"] == target)
                          or (direction == "forward" and e["to"] == target)],
                "certainty": "DEFINITE"}

    def data_structures(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        return [{"name": f["name"], "declaredType": f["type"],
                 "line": f["line"], "modifiers": f["modifiers"]}
                for f in t["fields"]]

    def data_structure_details(self, program, name):
        for d in self.data_structures(program) or []:
            if d["name"].lower() == name.lower():
                return d
        return None

    # -- Tier C -------------------------------------------------------------

    def dead_paragraphs(self, program):
        t = self._resolve_type(program)
        if not t:
            return None
        called = {c for m in t["methods"] for c in m["calls"]}
        return [{"name": m["name"], "line": m["line"]}
                for m in t["methods"] if m["name"] not in called
                and m["name"] not in ("close",)]

    def call_chain_statistics(self):
        return {"totalEdges": 1,
                "mostCalledModules": [{"name": "OrderRepository", "callCount": 1}]}

    def migration_impact(self, program):
        usage = self.ibmi_usage(program) or []
        ext = [u for u in usage if not u["resolved"]]
        return {"program": program, "fileCount": len(usage),
                "unresolvedDependencies": len(ext), "tables": [],
                "callers": 0,
                "risk": "LOW" if not ext else "MEDIUM"}

    def modernization_start(self, limit):
        rows = sorted(self.complex_programs(), key=lambda r: r["complexity"])
        return [{"program": r["program"], "fqn": r["fqn"],
                 "methods": r["methods"], "derived": 0,
                 "score": r["methods"]} for r in rows[:limit]]

    def auto_summary(self, program):
        exp = self.export(program)
        if not exp:
            return None
        return (f"{exp['kind']} {exp['program']} ({exp['fqn']}) in {exp['repo']}: "
                f"{len(exp['methods'])} methods, {len(exp['fields'])} fields.")
