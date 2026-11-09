#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Workspace bootstrap: restore a run into live services — THE only entry
point users need after cloning a codegraph-workspace.

    scripts/up.sh | scripts/up.ps1      (thin wrappers that find a Python)
      -> this file

What it does (workspace-restructure.md §6):
  1. python venv + deps for viewer/MCP servers (+ zstandard — no system zstd)
  2. npm install of the pinned codegraph SDK (needs Node >= 22.5 + npm)
  3. latest run's neo4j dump -> docker volume -> `codegraph-neo4j` container
  4. per-repo codegraph DBs -> snapshot/codegraphs/<repo>/.codegraph/
  5. repo clones checked out at the run's recorded commit SHAs
     (MFLens source reads + fileHashMatch drift keep working)
  6. runtime/config/*.json — MCP registration + per-service pointers
  7. graph viewer started (pid file in snapshot/)

Needs on the machine: Python 3.11+, Docker, Node >= 22.5, npm, git.
Cross-platform (macOS/Linux/Windows) — all logic lives here, not in sh/ps1.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import venv
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]   # <ws>/runtime
SCRIPTS = RUNTIME / "scripts"
WS = RUNTIME.parent                              # <ws>

NEO4J_IMAGE = "neo4j:5-community"


def consumer_names(settings_neo4j):
    """Consumer container/volume are per-workspace (derived from the ingest
    container name) so several workspaces can serve on one machine — and
    the pipeline knows exactly which consumer of ours to yield to."""
    base = settings_neo4j.get("container") or "codegraph-neo4j"
    return f"{base}-serve", f"{base}-serve-data"


def log(msg: str) -> None:
    print(f"[up] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[up] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------

def check_prereqs() -> None:
    if run(["docker", "--version"]).returncode != 0:
        die("docker not found — install Docker Desktop (or engine) first")
    ver = run(["node", "--version"]).stdout.strip().lstrip("v")
    if not ver:
        die("node not found — Node >= 22.5 is required (node:sqlite)")
    major_minor = tuple(int(x) for x in ver.split(".")[:2])
    if major_minor < (22, 5):
        die(f"node {ver} too old — >= 22.5 required (node:sqlite)")
    if run(["npm", "--version"]).returncode != 0:
        die("npm not found")


# ---------------------------------------------------------------------------
# python venv shared by viewer + both MCP servers
# ---------------------------------------------------------------------------

def venv_python() -> Path:
    bindir = RUNTIME / (".venv/Scripts" if os.name == "nt" else ".venv/bin")
    exe = "python.exe" if os.name == "nt" else "python"
    return bindir / exe


def ensure_venv() -> Path:
    """Create the runtime venv, install deps, then RE-EXEC into it (up.py
    itself needs zstandard + neo4j, which live in the venv — the system
    python only has to be able to run this file once)."""
    py = venv_python()
    if not py.exists():
        log("creating runtime venv …")
        venv.create(str(RUNTIME / ".venv"), with_pip=True)
    reqs = [RUNTIME / c / "requirements.txt"
            for c in ("mcp-server", "codegraph-mcp", "graph-viewer")]
    marker = RUNTIME / ".venv/.deps-installed"
    if not marker.exists():
        log("installing python deps …")
        cmd = [str(py), "-m", "pip", "install", "-q", "zstandard"]
        for r in reqs:
            if r.exists():
                cmd += ["-r", str(r)]
        res = run(cmd)
        if res.returncode != 0:
            die(f"pip install failed: {res.stderr[-500:]}")
        marker.write_text("ok\n")
    if Path(sys.executable).resolve() != py.resolve():
        os.execv(str(py), [str(py), str(Path(__file__).resolve()),
                           *sys.argv[1:]])
    return py


def ensure_codegraph_sdk() -> None:
    cg = RUNTIME / "codegraph-mcp"
    if (cg / "node_modules" / "@colbymchenry" / "codegraph").exists():
        return
    log("installing pinned codegraph SDK (npm) …")
    res = run(["npm", "install", "--prefix", str(cg)], cwd=str(cg))
    if res.returncode != 0:
        die(f"npm install failed: {res.stderr[-500:]}")


# ---------------------------------------------------------------------------
# neo4j restore
# ---------------------------------------------------------------------------

def restore_neo4j(dump_zst: Path, bolt_port: int, http_port: int,
                  password: str, container: str, volume: str) -> None:
    import zstandard
    # decompress into gitignored snapshot/ — NEVER next to the .zst files
    # (runs/ is committed content; raw dumps must not land there)
    log("decompressing neo4j dumps …")
    dumps_dir = WS / "snapshot" / "neo4j-dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    for zst in sorted(dump_zst.parent.glob("*.dump.zst")):
        with open(zst, "rb") as src, \
             open(dumps_dir / zst.name.removesuffix(".zst"), "wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)

    log("recreating neo4j volume + container …")
    run(["docker", "rm", "-f", container])
    run(["docker", "volume", "rm", "-f", volume])
    run(["docker", "volume", "create", volume])
    # system first (credentials), then the graph itself
    for db in ("system", "neo4j"):
        dump = dumps_dir / f"{db}.dump"
        if not dump.exists():
            continue
        res = run(["docker", "run", "--rm",
                   "-v", f"{dumps_dir}:/dumps",
                   "-v", f"{volume}:/data",
                   NEO4J_IMAGE, "neo4j-admin", "database", "load", db,
                   "--from-path=/dumps"])
        if res.returncode != 0:
            die(f"neo4j-admin load {db} failed: {res.stderr[-500:]}")
    res = run(["docker", "run", "-d", "--name", container,
               "-p", f"{bolt_port}:7687", "-p", f"{http_port}:7474",
               "-v", f"{volume}:/data",
               NEO4J_IMAGE])
    if res.returncode != 0:
        die(f"starting neo4j container failed: {res.stderr[-500:]}")

    log(f"waiting for bolt on :{bolt_port} …")
    from neo4j import GraphDatabase
    uri = f"bolt://localhost:{bolt_port}"
    for _ in range(60):
        try:
            with GraphDatabase.driver(uri, auth=("neo4j", password)) as d:
                d.verify_connectivity()
            log("neo4j ready")
            return
        except Exception:
            time.sleep(2)
    die("neo4j did not become ready within 120s")


# ---------------------------------------------------------------------------
# codegraph DBs + repo checkouts
# ---------------------------------------------------------------------------

def restore_codegraphs(run_dir: Path) -> Path:
    import zstandard
    snap = WS / "snapshot" / "codegraphs"
    for db_zst in sorted((run_dir / "codegraphs").glob("*/codegraph.db.zst")):
        name = db_zst.parent.name
        dest = snap / name / ".codegraph" / "codegraph.db"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(db_zst, "rb") as src, open(dest, "wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)
        log(f"codegraph[{name}] -> {dest.parent.parent}")
    return snap


def checkout_repos(run_manifest: dict) -> dict:
    """Ensure ws/repos/<name> is at the run's commitSha (MFLens source reads)."""
    repos = {}
    for entry in run_manifest.get("repos", []):
        name, url, sha = entry["name"], entry.get("cloneUrl"), entry.get("commitSha")
        path = WS / "repos" / name
        if not path.exists():
            if not url:
                log(f"repos[{name}]: no cloneUrl recorded — skipped")
                continue
            res = run(["git", "clone", url, str(path)])
            if res.returncode != 0:
                log(f"repos[{name}]: clone failed — skipped ({res.stderr[-120:]})")
                continue
        if sha:
            head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
            if head != sha:
                run(["git", "-C", str(path), "fetch", "origin", sha])
                run(["git", "-C", str(path), "checkout", "-f", sha])
        repos[name] = str(path)
        log(f"repos[{name}] @ {sha[:12] if sha else 'HEAD'}")
    return repos


# ---------------------------------------------------------------------------
# configs + viewer
# ---------------------------------------------------------------------------

def write_configs(bolt_port: int, password: str, viewer_port: int,
                  repo_paths: dict, snap: Path, mode: str) -> Path:
    cfg = RUNTIME / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    py = str(venv_python())

    (cfg / "mflens-repos.json").write_text(json.dumps(
        [{"name": n, "path": p} for n, p in repo_paths.items()], indent=2))

    # snapshot mode reads the materialized DBs; live mode reads working trees
    if mode == "live":
        cg_roots = {n: p for n, p in repo_paths.items()}
    else:
        cg_roots = {p.parent.parent.name: str(p.parent.parent)
                    for p in snap.glob("*/.codegraph/codegraph.db")}
    (cfg / "codegraph-repos.json").write_text(json.dumps(
        [{"name": n, "path": p} for n, p in cg_roots.items()], indent=2))

    mcp = {
        "mcpServers": {
            "mflens": {
                "command": py,
                "args": [str(RUNTIME / "mcp-server" / "server.py")],
                "env": {
                    "MCP_NEO4J_URI": f"bolt://localhost:{bolt_port}",
                    "MCP_NEO4J_PASSWORD": password,
                    "MCP_REPOS_JSON": str(cfg / "mflens-repos.json"),
                },
            },
            "codegraph-portfolio": {
                "command": py,
                "args": [str(RUNTIME / "codegraph-mcp" / "server.py")],
                "env": {
                    "MRG_REPOS_JSON": str(cfg / "codegraph-repos.json"),
                    "MRG_NODE": "node",
                },
            },
        }
    }
    reg = cfg / "mcp.json"
    reg.write_text(json.dumps(mcp, indent=2))
    return reg


def start_viewer(py: Path, bolt_port: int, password: str, viewer_port: int) -> None:
    stop_viewer()
    env = {**os.environ,
           "NEO4J_URI": f"bolt://localhost:{bolt_port}",
           "NEO4J_PASSWORD": password,
           "PORT": str(viewer_port)}
    logs = WS / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "viewer.log", "ab")
    proc = subprocess.Popen(
        [str(py), "viewer_server.py"],
        cwd=str(RUNTIME / "graph-viewer"), env=env,
        stdout=out, stderr=out)
    (WS / "snapshot" / "viewer.pid").parent.mkdir(parents=True, exist_ok=True)
    (WS / "snapshot" / "viewer.pid").write_text(str(proc.pid))
    log(f"viewer pid={proc.pid} -> http://localhost:{viewer_port}")


def stop_viewer() -> None:
    pid_file = WS / "snapshot" / "viewer.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        if os.name == "nt":
            run(["taskkill", "/F", "/PID", str(pid)])
        else:
            os.kill(pid, 15)
        time.sleep(1)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    finally:
        pid_file.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default=None,
                    help="run id (default: latest.json)")
    ap.add_argument("--mode", choices=["snapshot", "live"], default="snapshot",
                    help="codegraph source: materialized snapshot or working trees")
    ap.add_argument("--no-viewer", action="store_true")
    ap.add_argument("--skip-repos", action="store_true",
                    help="skip repo checkouts (MFLens source reads disabled)")
    args = ap.parse_args()

    check_prereqs()
    settings = json.loads((WS / "workspace.json").read_text()).get("settings", {})
    neo4j = settings.get("neo4j", {})
    bolt = int(neo4j.get("boltPort", 7688))
    http = int(neo4j.get("httpPort", 7475))
    password = neo4j.get("password", "graphpassword123")
    viewer_port = int(settings.get("viewerPort", 7600))

    run_id = args.run
    if not run_id:
        run_id = json.loads((WS / "latest.json").read_text())["run"]
    run_dir = WS / "runs" / run_id
    if not run_dir.exists():
        die(f"run {run_id} not found under runs/")
    run_manifest = json.loads((run_dir / "run-manifest.json").read_text())
    log(f"run {run_id} ({run_manifest.get('verification', {}).get('status')})")

    py = ensure_venv()
    ensure_codegraph_sdk()
    container, volume = consumer_names(neo4j)
    restore_neo4j(run_dir / "neo4j" / "neo4j.dump.zst", bolt, http,
                  password, container, volume)
    snap = restore_codegraphs(run_dir)
    repo_paths = {} if args.skip_repos else checkout_repos(run_manifest)
    reg = write_configs(bolt, password, viewer_port, repo_paths, snap, args.mode)
    if not args.no_viewer:
        start_viewer(py, bolt, password, viewer_port)

    print()
    log("workspace is up:")
    print(f"    neo4j browser : http://localhost:{http}  (bolt :{bolt})")
    if not args.no_viewer:
        print(f"    graph viewer  : http://localhost:{viewer_port}")
    print(f"    MCP registration: {reg}")
    print(    "      (add the mcpServers block to your ZCode/Claude config)")


if __name__ == "__main__":
    main()
