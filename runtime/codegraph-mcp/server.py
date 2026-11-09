#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Codegraph portfolio MCP — the fan-out wrapper over per-repo codegraph
indexes (design.md §10: "search/callers/impact/explore across all repos").

Complements — never replaces — the MFLens 60+4 contract (mcp-server/):
MFLens answers portfolio-graph questions (types, members, inheritance,
name-level CALLS over Neo4j); this server answers CODEGRAPH questions
(FTS search, resolved callers, impact radius, task context) across every
repo's `.codegraph/` index in one call.

Five tools, each fanning out via fanout.mjs (Node + pinned codegraph SDK):
  portfolio_search(query, limit?)          — FTS symbol search, all repos
  portfolio_symbol(qualified_name)         — exact identity lookup per repo
  portfolio_callers(name, repo?)           — resolved callers of a callable
  portfolio_impact(name, depth?, repo?)    — impact radius per repo
  portfolio_explore(query, max_nodes?)     — codegraph buildContext per repo

Run:
  .venv/bin/python server.py                # stdio
Repos config (same format as MFLens repos.json): MRG_REPOS_JSON,
default `repos.json` next to this file.
"""

import json
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).resolve().parent
FANOUT = SCRIPT_DIR / "fanout.mjs"
REPOS_JSON = Path(os.environ.get("MRG_REPOS_JSON", SCRIPT_DIR / "repos.json"))
NODE = os.environ.get("MRG_NODE", "node")

mcp = FastMCP("codegraph-portfolio", instructions=(
    "Portfolio-wide codegraph access: fan-out search/callers/impact/explore "
    "over every repo's codegraph index. Results are tagged with the repo "
    "they came from. Complements the MFLens Neo4j MCP, does not replace it."))


def _fanout(command: str, args: dict) -> dict:
    """Run fanout.mjs and return its parsed JSON (errors per repo are
    carried in results[].error; a total failure raises)."""
    proc = subprocess.run(
        [NODE, str(FANOUT), str(REPOS_JSON), command, json.dumps(args)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"fanout {command} failed ({proc.returncode}): "
            f"{proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _ok(payload: dict) -> dict:
    """Shape shared by every tool: per-repo results + a failures list."""
    failures = [f"{r['repo']}: {r['error']}" for r in payload["results"]
                if r.get("error")]
    return {"command": payload["command"], "repos": payload["repos"],
            "failures": failures, "results": payload["results"]}


@mcp.tool()
def portfolio_search(query: str, limit: int = 20) -> dict:
    """Full-text symbol search across ALL repos' codegraph indexes.

    Every hit carries its repo, kind, qualifiedName, file and line. The same
    simple name in two repos yields two hits (they are distinct symbols)."""
    return _ok(_fanout("search", {"query": query, "limit": limit}))


@mcp.tool()
def portfolio_symbol(qualified_name: str) -> dict:
    """Exact qualified-name lookup per repo (the identity that survives a
    re-index). Returns every overload/nesting site, tagged by repo."""
    return _ok(_fanout("symbol", {"qualifiedName": qualified_name}))


@mcp.tool()
def portfolio_callers(name: str, repo: str = "") -> dict:
    """Resolved callers of a method/function name across the portfolio
    (all overloads, all repos unless `repo` narrows it). Unlike MFLens's
    name-level CALLS, these edges are codegraph-resolved call sites."""
    args = {"name": name}
    if repo:
        args["repo"] = repo
    return _ok(_fanout("callers", args))


@mcp.tool()
def portfolio_impact(name: str, depth: int = 2, repo: str = "") -> dict:
    """Impact radius (who is affected transitively) of a callable name,
    per repo. depth bounds the traversal."""
    args = {"name": name, "depth": depth}
    if repo:
        args["repo"] = repo
    return _ok(_fanout("impact", args))


@mcp.tool()
def portfolio_explore(query: str, max_nodes: int = 40) -> dict:
    """Codegraph task-context per repo: entry points + summary for a natural
    language query, fanned out over all repos."""
    return _ok(_fanout("explore", {"query": query, "maxNodes": max_nodes}))


if __name__ == "__main__":
    mcp.run()
