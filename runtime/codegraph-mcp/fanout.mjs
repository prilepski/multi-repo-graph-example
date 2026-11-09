#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
// Portfolio fan-out over per-repo codegraph indexes (design.md §10):
// opens every repo's codegraph DB read-only, runs ONE query shape per
// invocation, merges results tagged with the repo name. Complements — never
// replaces — the MFLens 60+4 contract.
//
// Usage: node fanout.mjs <reposJsonPath> <command> [argsJson]
//   reposJson: [{"name","path"}, ...]   (same format as MFLens repos.json)
//   commands:
//     search   {query, limit?=20, kinds?, languages?}  — searchNodes per repo
//     symbol   {qualifiedName}                          — identity lookup
//     callers  {name, repo?}                            — who calls this name
//     impact   {name, depth?=2, repo?}                  — impact radius
//     explore  {query, maxNodes?=40}                    — buildContext per repo
//
// Stdout: one JSON object {command, repos, results: [{repo, ...}]}.
// A repo that fails (no index, bad db) yields {repo, error} and does not
// fail the run; exit 1 only if EVERY repo failed. The codegraph SDK is
// resolved from indexer/nodejs/node_modules (single pinned install).
import { readFileSync } from 'node:fs';

// Resolve the codegraph SDK for BOTH layouts:
//   1. MRG_CODEGRAPH_SDK env var (explicit pointer — used by the workspace runtime)
//   2. ./node_modules (workspace runtime layout: up.py npm-installs here)
//   3. ../indexer/nodejs/node_modules (tool-repo layout, single pinned install)
async function importSdk() {
  const candidates = [
    process.env.MRG_CODEGRAPH_SDK,
    new URL('./node_modules/@colbymchenry/codegraph/npm-sdk.js',
            import.meta.url).href,
    new URL('../indexer/nodejs/node_modules/@colbymchenry/codegraph/npm-sdk.js',
            import.meta.url).href,
  ].filter(Boolean);
  let lastErr;
  for (const url of candidates) {
    try {
      const mod = await import(url);      // CJS: exports under .default
      const { CodeGraph } = mod.default ?? mod;
      if (CodeGraph) return CodeGraph;
    } catch (err) { lastErr = err; }
  }
  throw new Error(`codegraph SDK not found (tried ${candidates.length} locations): ${lastErr}`);
}
const CodeGraph = await importSdk();

const [reposJsonPath, command, argsJson = '{}'] = process.argv.slice(2);
if (!reposJsonPath || !command) {
  console.error('usage: node fanout.mjs <reposJsonPath> <command> [argsJson]');
  process.exit(2);
}
const repos = JSON.parse(readFileSync(reposJsonPath, 'utf-8'));
const args = JSON.parse(argsJson);
const CALLABLE = new Set(['method', 'function']);

async function withRepo(name, path, fn) {
  let cg;
  try {
    cg = await CodeGraph.open(path, { readOnly: true });
    return { repo: name, ...(await fn(cg)) };
  } catch (err) {
    return { repo: name, error: String(err?.message || err) };
  } finally {
    try { cg?.close(); } catch { /* already closed */ }
  }
}

// resolve a symbol NAME to callable nodes (all overloads, capped)
function resolveCallable(cg, name, cap = 8) {
  return cg.getNodesByName(name)
    .filter(n => CALLABLE.has(n.kind))
    .slice(0, cap);
}

const COMMANDS = {
  async search(cg) {
    const hits = cg.searchNodes(args.query, {
      limit: args.limit ?? 20,
      ...(args.kinds ? { kinds: args.kinds } : {}),
      ...(args.languages ? { languages: args.languages } : {}),
    });
    return { hits: hits.map(h => ({
      kind: h.node.kind, name: h.node.name,
      qualifiedName: h.node.qualifiedName, filePath: h.node.filePath,
      language: h.node.language, line: h.node.startLine, score: h.score,
    })) };
  },

  async symbol(cg) {
    const nodes = cg.getNodesByQualifiedName(args.qualifiedName);
    return { nodes: nodes.map(n => ({
      kind: n.kind, name: n.name, qualifiedName: n.qualifiedName,
      filePath: n.filePath, language: n.language,
      line: n.startLine, signature: n.signature ?? null,
    })) };
  },

  async callers(cg) {
    const out = [];
    for (const node of resolveCallable(cg, args.name)) {
      for (const { node: caller } of cg.getCallers(node.id)) {
        out.push({ callee: node.qualifiedName,
                   caller: caller.qualifiedName,
                   callerKind: caller.kind, filePath: caller.filePath,
                   line: caller.startLine });
      }
    }
    return { callers: out };
  },

  async impact(cg) {
    const depth = args.depth ?? 2;
    const out = [];
    for (const node of resolveCallable(cg, args.name, 4)) {
      const sub = cg.getImpactRadius(node.id, depth);
      out.push({ symbol: node.qualifiedName, depth,
                 impacted: [...sub.nodes.values()]
                   .filter(n => n.id !== node.id)
                   .slice(0, 100)
                   .map(n => ({ kind: n.kind, qualifiedName: n.qualifiedName,
                                filePath: n.filePath })),
                 edgeCount: sub.edges.length });
    }
    return { impact: out };
  },

  async explore(cg) {
    // with format:'json' buildContext returns a STRING of the TaskContext
    const raw = await cg.buildContext(args.query, {
      format: 'json', maxNodes: args.maxNodes ?? 40, includeCode: false,
    });
    const ctx = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return { summary: ctx.summary,
             entryPoints: (ctx.entryPoints ?? []).map(n => ({
               kind: n.kind, qualifiedName: n.qualifiedName,
               filePath: n.filePath,
             })) };
  },
};

const handler = COMMANDS[command];
if (!handler) {
  console.error(`unknown command ${command} (expected: ${Object.keys(COMMANDS).join(', ')})`);
  process.exit(2);
}

const selected = args.repo ? repos.filter(r => r.name === args.repo) : repos;
const results = [];
for (const r of selected) {
  results.push(await withRepo(r.name, r.path, (cg) => handler(cg)));
}
const okCount = results.filter(r => !r.error).length;
process.stdout.write(JSON.stringify({
  command, repos: selected.map(r => r.name), results,
}) + "\n");
process.exit(okCount === 0 && selected.length > 0 ? 1 : 0);
