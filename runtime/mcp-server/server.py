#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""MFLens-compatible MCP server over the Java code graph.

Implements all 60 tools from the MFLens tool inventory
(mflens-tool-inventory.md), backed by the Neo4j code graph produced by
java-indexer and the cloned repository sources.

Domain mapping: program = Java type; paragraph = method; data structure =
field; lineage = extends/implements/derived edges. Tools whose domain does
not exist in a Java portfolio (CICS, IMS, MQ, JCL/CL, COBOL conversion,
SQL tables) return the documented empty shapes.

Run:
  .venv/bin/python server.py                 # stdio (for .zcode/.mcp.json)
  MCP_TRANSPORT=http .venv/bin/python server.py   # streamable HTTP :8790/mcp
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import backend as back

SCRIPT_DIR = Path(__file__).resolve().parent

mcp = FastMCP("MFlens", instructions=(
    "Java code-graph MCP exposing the 60-tool MFLens inventory. "
    "Program = Java class/interface/enum/record; paragraph = method; "
    "data structure = field; lineage = inheritance/derived edges."))


def make_backend():
    if os.environ.get("MCPLENS_FAKE"):  # test/preview mode
        return back.FakeBackend(Path(os.environ["MCPLENS_FAKE_ROOT"]
                                     if os.environ.get("MCPLENS_FAKE_ROOT")
                                     else SCRIPT_DIR / "tests" / "fixture-src"))
    return back.Neo4jBackend(
        uri=os.environ.get("MCP_NEO4J_URI", "bolt://localhost:7687"),
        password=os.environ.get("MCP_NEO4J_PASSWORD", "graphpassword123"),
        repos_json=os.environ.get(
            "MCP_REPOS_JSON",
            str(SCRIPT_DIR.parent / "java-indexer" / "repos.json")),
    )


B = make_backend()


def _not_found(what: str, key: str):
    return {"error": f"{what} not found: {key}"}


# ===========================================================================
# Tier A — 6 tools
# ===========================================================================

@mcp.tool()
def search_program(programName: str) -> dict:
    """Find programs (Java types) by exact or prefix name; returns fqn, kind, repository."""
    r = B.search_types(programName)
    return {"matches": r, "count": len(r)}


@mcp.tool()
def search_component(prefix: str) -> dict:
    """Prefix search over program (type) names."""
    r = B.search_types_prefix(prefix)
    return {"matches": r, "count": len(r)}


@mcp.tool()
def get_program_source_code(programName: str, startLine: int, endLine: int) -> dict:
    """Exact source text of a physical line range of the program's file.
    fileHashMatch=false means the file on disk has drifted from the indexed graph."""
    r = B.get_source(programName, startLine, endLine)
    if r is None:
        return _not_found("program", programName)
    return {"program": programName, "startLine": startLine, "endLine": endLine,
            "code": r["code"], "file": r["file"], "fileHashMatch": r["hashMatch"]}


@mcp.tool()
def search_code(programName: str, searchText: str, maxResults: int = 100) -> dict:
    """Search text within one program's source; physical line numbers.
    fileHashMatch=false means the file on disk has drifted from the indexed graph."""
    r = B.search_in_file(programName, searchText, maxResults)
    if r is None:
        return _not_found("program", programName)
    return {"program": programName, "file": r["file"], "fileHashMatch": r["hashMatch"],
            "hits": r["hits"], "count": len(r["hits"])}


@mcp.tool()
def search_code_portfolio(searchText: str, maxResultsPerProgram: int = 0) -> dict:
    """Portfolio-wide text search across all indexed files (0 = unlimited per program)."""
    hits = B.search_portfolio_text(searchText, maxResultsPerProgram)
    return {"hits": hits, "count": len(hits)}


@mcp.tool()
def get_program_deep_call_chain(programName: str, maxDepth: int = 10) -> dict:
    """Resolved call tree from the program's methods (name-level resolution)."""
    chain = B.deep_call_chain(programName, maxDepth)
    if chain is None:
        return _not_found("program", programName)
    return chain


# ===========================================================================
# Tier B — 24 tools
# ===========================================================================

@mcp.tool()
def get_portfolio_overview() -> dict:
    """Portfolio inventory: repositories, files, programs, members, dependencies."""
    return B.overview()


@mcp.tool()
def get_portfolio_files() -> dict:
    """All indexed source files with the types they define."""
    return {"files": B.portfolio_files()}


@mcp.tool()
def get_portfolio_assets() -> dict:
    """Asset counts: programs, packages, copybooks (0), JCL jobs (0)."""
    return B.assets()


@mcp.tool()
def get_complex_programs() -> dict:
    """Programs ranked by complexity proxy (methods + call volume)."""
    return {"programs": B.complex_programs()}


@mcp.tool()
def get_program_summary(programName: str) -> dict:
    """Program summary (stored annotation if set, else auto-generated from the graph)."""
    stored = B.get_annotation(programName, "summary")
    auto = B.auto_summary(programName)
    if auto is None:
        return _not_found("program", programName)
    return {"program": programName, "summary": stored or auto,
            "source": "stored" if stored else "auto-generated"}


@mcp.tool()
def get_program_prd(programName: str) -> dict:
    """Program requirements documentation (stored annotation or auto facts)."""
    stored = B.get_annotation(programName, "prd")
    exp = B.export(programName)
    if exp is None:
        return _not_found("program", programName)
    auto = (f"{exp['kind']} {exp['fqn']} in {exp['repo']}, file {exp['file']}; "
            f"{len(exp['methods'])} methods, {len(exp['fields'])} fields; "
            f"bases: {', '.join(b['target'] for b in exp['bases']) or 'none'}.")
    return {"program": programName, "documentation": stored or auto,
            "source": "stored" if stored else "auto-generated"}


@mcp.tool()
def get_program_export(programName: str) -> dict:
    """Everything the graph knows about one program (methods, fields, bases, imports)."""
    exp = B.export(programName)
    if exp is None:
        return _not_found("program", programName)
    return exp


@mcp.tool()
def get_program_paragraphs(programName: str) -> dict:
    """Methods (paragraphs) of a program with accurate start/end lines."""
    spans = B.paragraphs(programName)
    if spans is None:
        return _not_found("program", programName)
    return {"program": programName, "paragraphs": spans, "count": len(spans)}


@mcp.tool()
def get_program_paragraph_details(programName: str, paragraphName: str) -> dict:
    """Full source slice of one method."""
    d = B.paragraph_details(programName, paragraphName)
    if d is None:
        return _not_found("paragraph", f"{programName}#{paragraphName}")
    d["fileHashMatch"] = d.pop("hashMatch")
    return d


@mcp.tool()
def get_control_flow_graph(programName: str, packageName: str = "") -> dict:
    """Intra-program method call graph."""
    cfg = B.control_flow(programName, packageName or None)
    if cfg is None:
        return _not_found("program", programName)
    return cfg


@mcp.tool()
def get_portfolio_call_chain(page: int = 0, pageSize: int = 100) -> dict:
    """Paged portfolio call graph between programs (deduped program-level edges)."""
    return B.call_chain_page(page, pageSize)


@mcp.tool()
def find_portfolio_call_paths(fromProgram: str, toProgram: str = "",
                              minDepth: int = 1, maxDepth: int = 5) -> dict:
    """Call paths between programs in the portfolio."""
    return {"paths": B.find_call_paths(fromProgram, toProgram, minDepth, maxDepth)}


@mcp.tool()
def get_portfolio_sql_tables() -> dict:
    """SQL-referenced tables (none in a Java graph portfolio)."""
    return {"tables": []}


@mcp.tool()
def get_sql_table_usage(tableName: str) -> dict:
    """Programs using a SQL table."""
    return {"table": tableName, "usage": [], "note": "no SQL table model in this portfolio"}


@mcp.tool()
def get_sql_table_structure(tableName: str) -> dict:
    """Column structure of a SQL table."""
    return {"table": tableName, "columns": [],
            "note": "no SQL table model in this portfolio"}


@mcp.tool()
def get_table_column_usage(tableName: str) -> dict:
    """Per-column usage of a SQL table."""
    return {"table": tableName, "columns": [],
            "note": "no SQL table model in this portfolio"}


@mcp.tool()
def get_table_lineage(tableName: str) -> dict:
    """Table-to-table lineage."""
    return {"table": tableName, "lineage": [],
            "note": "no SQL table model in this portfolio"}


@mcp.tool()
def get_program_sql_statements(programName: str, tableName: str = "") -> dict:
    """SQL statements in a program (none extracted for Java sources)."""
    return {"program": programName, "tableName": tableName, "statements": [],
            "note": "SQL extraction not applicable to this portfolio"}


@mcp.tool()
def get_ibmi_usage(programName: str) -> dict:
    """External references (imports) of a program with resolution state."""
    usage = B.ibmi_usage(programName)
    if usage is None:
        return _not_found("program", programName)
    return {"program": programName, "usage": usage}


@mcp.tool()
def list_lineage_sources(programName: str, includeInternal: bool = False) -> dict:
    """Lineage sources of a program (its bases: extends/implements)."""
    src = B.lineage_sources(programName, includeInternal)
    if src is None:
        return _not_found("program", programName)
    return {"program": programName, "sources": src}


@mcp.tool()
def list_lineage_targets(programName: str, includeInternal: bool = False) -> dict:
    """Lineage targets of a program (derived types / callers)."""
    tgt = B.lineage_targets(programName, includeInternal)
    if tgt is None:
        return _not_found("program", programName)
    return {"program": programName, "targets": tgt}


@mcp.tool()
def trace_target_lineage(targetName: str, direction: str = "forward",
                         maxDepth: int = 5) -> dict:
    """Trace inheritance lineage forward or backward from a target FQN."""
    return B.trace_lineage(targetName, direction, maxDepth)


@mcp.tool()
def list_data_structures(programName: str) -> dict:
    """Declared fields of a program (accurate types from the parse)."""
    ds = B.data_structures(programName)
    if ds is None:
        return _not_found("program", programName)
    return {"program": programName, "structures": ds}


@mcp.tool()
def get_data_structure_details(programName: str, structureName: str) -> dict:
    """One field's details."""
    d = B.data_structure_details(programName, structureName)
    if d is None:
        return _not_found("data structure", f"{programName}.{structureName}")
    return d


# ===========================================================================
# Tier C — 30 tools
# ===========================================================================

@mcp.tool()
def get_program_dead_paragraphs(programName: str) -> dict:
    """Methods never cited as call targets in their repository (graph-computed)."""
    dead = B.dead_paragraphs(programName)
    if dead is None:
        return _not_found("program", programName)
    return {"program": programName, "deadParagraphs": dead}


@mcp.tool()
def get_portfolio_call_chain_statistics() -> dict:
    """Aggregated call-graph statistics (properly computed)."""
    return B.call_chain_statistics()


@mcp.tool()
def get_ibmi_usage_portfolio() -> dict:
    """Portfolio-level file-usage aggregation."""
    return B.ibmi_usage_portfolio()


@mcp.tool()
def get_program_outputs(programName: str) -> dict:
    """Legacy outputs model — not modelled for this portfolio."""
    return {"program": programName, "outputs": {},
            "note": "outputs model not applicable to Java sources"}


@mcp.tool()
def get_lineage_summary(programName: str) -> dict:
    """Legacy lineage summary — use list_lineage_sources/targets instead."""
    src = B.lineage_sources(programName, True)
    if src is None:
        return _not_found("program", programName)
    return {"program": programName, "sources": len(src), "targets": 0,
            "note": "legacy summary; prefer list_lineage_sources/targets"}


@mcp.tool()
def get_field_lineage(fieldName: str, programName: str = "") -> dict:
    """Field-level lineage — not modelled for this portfolio."""
    return {"field": fieldName, "program": programName, "transformations": [],
            "note": "field lineage not applicable to Java sources"}


@mcp.tool()
def trace_field_lineage(fieldName: str, programName: str) -> dict:
    """Trace one field's lineage — not modelled for this portfolio."""
    return {"field": fieldName, "program": programName, "lineage": [],
            "note": "field lineage not applicable to Java sources"}


@mcp.tool()
def trace_output_structure(outputName: str) -> dict:
    """Trace an output structure — not modelled for this portfolio."""
    return {"output": outputName, "structure": {},
            "note": "outputs model not applicable to Java sources"}


@mcp.tool()
def get_business_capabilities() -> dict:
    """Business capability classification — nothing classified."""
    return {"capabilities": []}


@mcp.tool()
def get_programs_by_capabilities(capabilities: list[str]) -> dict:
    """Programs by capability — nothing classified."""
    return {"capabilities": capabilities, "programs": []}


@mcp.tool()
def recommend_modernization_start(limit: int = 5) -> dict:
    """Programs safest to modernize first (fewest members/dependents, graph-computed)."""
    return {"recommendations": B.modernization_start(limit)}


@mcp.tool()
def analyze_migration_impact(programName: str) -> dict:
    """Migration impact of a program (imports/callers/risk, graph-computed)."""
    impact = B.migration_impact(programName)
    if impact is None:
        return _not_found("program", programName)
    return impact


@mcp.tool()
def get_cics_usage(programName: str) -> dict:
    """CICS usage — not applicable outside z/OS."""
    return {"program": programName, "transactions": [], "count": 0}


@mcp.tool()
def get_cics_usage_portfolio() -> dict:
    """Portfolio CICS usage — not applicable."""
    return {"count": 0, "programs": []}


@mcp.tool()
def get_cics_transactions() -> dict:
    """CICS transaction catalog — not applicable."""
    return {"transactions": [], "catalogEmpty": True}


@mcp.tool()
def get_ims_usage(programName: str) -> dict:
    """IMS usage — not applicable on this platform."""
    return {"program": programName, "databases": [], "count": 0}


@mcp.tool()
def get_ims_databases() -> dict:
    """IMS databases — not applicable."""
    return {"databases": []}


@mcp.tool()
def get_portfolio_ims_segments() -> dict:
    """IMS segments — not applicable."""
    return {"segments": []}


@mcp.tool()
def get_mq_usage_program(programName: str) -> dict:
    """IBM MQ usage — Java messaging not modelled; use search_code_portfolio."""
    return {"program": programName, "queues": [], "count": 0}


@mcp.tool()
def get_mq_usage_portfolio() -> dict:
    """Portfolio MQ usage — not modelled."""
    return {"count": 0, "programs": []}


@mcp.tool()
def list_portfolio_jcl_jobs() -> dict:
    """JCL jobs — no JCL/CL members in this portfolio."""
    return {"jobs": [], "count": 0}


@mcp.tool()
def get_jcl_job_execution_details(jobName: str) -> dict:
    """JCL job details — no JCL in this portfolio."""
    return _not_found("job", jobName)


@mcp.tool()
def get_jcl_job_io(jobName: str) -> dict:
    """JCL job I/O — no JCL in this portfolio."""
    return _not_found("job", jobName)


@mcp.tool()
def get_jcl_bottom_up_file_analysis(jobName: str) -> dict:
    """Bottom-up file analysis — no JCL in this portfolio."""
    return _not_found("job", jobName)


@mcp.tool()
def map_jcl_job_files_to_cobol_data_structures(jobName: str) -> dict:
    """JCL→COBOL mapping — no JCL or COBOL in this portfolio."""
    return _not_found("job", jobName)


@mcp.tool()
def search_jcl_source(searchText: str) -> dict:
    """Search JCL sources — none in this portfolio."""
    return {"hits": [], "count": 0}


@mcp.tool()
def cobol_ibm_to_iso(programName: str) -> dict:
    """COBOL IBM→ISO conversion — COBOL only."""
    return {"error": "COBOL only; not applicable to this portfolio"}


# -- Mutating (3): local annotations store ---------------------------------

@mcp.tool()
def update_program_summary(programName: str, summary: str) -> dict:
    """Store a program summary (local annotations store)."""
    B.set_summary(programName, summary)
    return {"status": "updated", "program": programName, "field": "summary"}


@mcp.tool()
def update_program_prd_documentation(programName: str, documentation: str) -> dict:
    """Store program PRD documentation (local annotations store)."""
    B.set_prd(programName, documentation)
    return {"status": "updated", "program": programName, "field": "prd"}


@mcp.tool()
def update_program_classification(programName: str, classification: str) -> dict:
    """Store a program classification (local annotations store)."""
    B.set_classification(programName, classification)
    return {"status": "updated", "program": programName, "field": "classification"}


# ===========================================================================
# Extension tools — inventory & code statistics (design §6)
# The MFLens 60-tool set above stays intact; these are additions.
# ===========================================================================

@mcp.tool()
def get_portfolio_file_statistics(repository: str = "") -> dict:
    """Coverage per repository: tracked vs on-disk files, deep-indexed vs
    inventory-only counts, binaries (from the latest StatsSnapshot + live facts)."""
    return B.file_statistics(repository or None)


@mcp.tool()
def get_file_categories(repository: str = "", pathPrefix: str = "",
                        kind: str = "") -> dict:
    """File counts and bytes by category (kind -> category taxonomy);
    optionally scoped to one repository, directory prefix, or kind."""
    rows = B.file_categories(repository or None, pathPrefix or None, kind or None)
    return {"rows": rows, "totalFiles": sum(r["files"] for r in rows)}


@mcp.tool()
def get_loc_statistics(repository: str = "", category: str = "",
                       pathPrefix: str = "") -> dict:
    """Lines of code by category with code/comment/blank split; optionally
    scoped to one repository, category, or directory prefix."""
    return B.loc_statistics(repository or None, category or None, pathPrefix or None)


@mcp.tool()
def get_file_details(path: str, repository: str) -> dict:
    """Full inventory record for one file: category, role, bytes, LOC split,
    content hash and drift (fileHashMatch), deepIndexed flag."""
    d = B.file_details(path, repository)
    if d is None:
        return _not_found("file", f"{repository}:{path}")
    return d


# ===========================================================================
# Inventory check helper (used by tests)
# ===========================================================================

TOOL_INVENTORY = [
    # Tier A
    "search_program", "search_component", "get_program_source_code", "search_code",
    "search_code_portfolio", "get_program_deep_call_chain",
    # Tier B
    "get_portfolio_overview", "get_portfolio_files", "get_portfolio_assets",
    "get_complex_programs", "get_program_summary", "get_program_prd",
    "get_program_export", "get_program_paragraphs", "get_program_paragraph_details",
    "get_control_flow_graph", "get_portfolio_call_chain", "find_portfolio_call_paths",
    "get_portfolio_sql_tables", "get_sql_table_usage", "get_sql_table_structure",
    "get_table_column_usage", "get_table_lineage", "get_program_sql_statements",
    "get_ibmi_usage", "list_lineage_sources", "list_lineage_targets",
    "trace_target_lineage", "list_data_structures", "get_data_structure_details",
    # Tier C
    "get_program_dead_paragraphs", "get_portfolio_call_chain_statistics",
    "get_ibmi_usage_portfolio", "get_program_outputs", "get_lineage_summary",
    "get_field_lineage", "trace_field_lineage", "trace_output_structure",
    "get_business_capabilities", "get_programs_by_capabilities",
    "recommend_modernization_start", "analyze_migration_impact",
    "get_cics_usage", "get_cics_usage_portfolio", "get_cics_transactions",
    "get_ims_usage", "get_ims_databases", "get_portfolio_ims_segments",
    "get_mq_usage_program", "get_mq_usage_portfolio",
    "list_portfolio_jcl_jobs", "get_jcl_job_execution_details", "get_jcl_job_io",
    "get_jcl_bottom_up_file_analysis", "map_jcl_job_files_to_cobol_data_structures",
    "search_jcl_source", "cobol_ibm_to_iso",
    "update_program_summary", "update_program_prd_documentation",
    "update_program_classification",
]
EXTENSION_TOOLS = [
    "get_portfolio_file_statistics",
    "get_file_categories",
    "get_loc_statistics",
    "get_file_details",
]
ALL_TOOLS = TOOL_INVENTORY + EXTENSION_TOOLS
assert len(TOOL_INVENTORY) == 60, len(TOOL_INVENTORY)
assert len(ALL_TOOLS) == 64, len(ALL_TOOLS)


async def list_tool_names() -> list[str]:
    tools = await mcp.list_tools()
    return sorted(t.name for t in tools)


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT") == "http":
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8790"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
