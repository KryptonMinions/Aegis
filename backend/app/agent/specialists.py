"""SpecialistConfig registry (ORCHESTRATOR_STEERING.md §4).

The full spec's tool table is built: run_sql/get_schema
(P1), search_narratives (RAG, via the embeddings service), resolve_entity/get_case (P2), mo_match/build_network/geo_query/
trend_series (P3), build_pack_report (post-P6, closing the pack_report gap
found in the demo pass — see E2E_TESTING_GAPS.md §2/§5b/§6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .tools.analysis import MoMatchTool, TrendSeriesTool
from .tools.base import Tool
from .tools.entities import GetCaseTool, ResolveEntityTool
from .tools.geo import GeoQueryTool
from .tools.network import BuildNetworkTool
from .tools.reports import BuildPackReportTool
from .tools.retrieval import SearchNarrativesTool
from .tools.sql import GetSchemaTool, RunSqlTool

BlockType = Literal[
    "text", "table", "case_card", "mo_match", "network_graph", "map", "pack_report", "no_answer"
]


@dataclass(frozen=True)
class SpecialistConfig:
    name: str
    profile: Literal["fast", "smart"]
    max_iterations: int
    tools: list[Tool]
    allowed_blocks: frozenset[BlockType]
    prompt_file: str
    sql_scope: Literal["case", "audit"] = "case"


# Stateless tool instances, safe to share across turns/specialists.
_run_sql = RunSqlTool()
_get_schema = GetSchemaTool()
_resolve_entity = ResolveEntityTool()
_get_case = GetCaseTool()
_search_narratives = SearchNarrativesTool()
_mo_match = MoMatchTool()
_build_network = BuildNetworkTool()
_geo_query = GeoQueryTool()
_trend_series = TrendSeriesTool()
_build_pack_report = BuildPackReportTool()

CASE_INVESTIGATOR = SpecialistConfig(
    name="case_investigator",
    profile="fast",
    # Bumped 6->8->10 on live evidence, not guesswork: a thorough antecedents
    # check (history sheet + FIR involvement + narrative entries + gang
    # membership = ~7 tool calls) ran out of budget at 8 with zero iterations
    # left to write the final answer, and no payload registered to fall back
    # on (none of run_sql/get_schema/resolve_entity register one).
    max_iterations=10,
    tools=[_run_sql, _get_schema, _search_narratives, _resolve_entity, _get_case, _mo_match],
    allowed_blocks=frozenset({"text", "table", "case_card", "mo_match", "no_answer"}),
    prompt_file="case_investigator.v1.md",
)

INTEL_ANALYST = SpecialistConfig(
    name="intel_analyst",
    profile="fast",
    max_iterations=8,
    tools=[_run_sql, _get_schema, _resolve_entity, _build_network, _geo_query, _trend_series],
    allowed_blocks=frozenset({"text", "table", "network_graph", "map", "no_answer"}),
    prompt_file="intel_analyst.v1.md",
)

REPORT_COMPOSER = SpecialistConfig(
    name="report_composer",
    profile="smart",
    max_iterations=10,
    tools=[
        _run_sql, _get_schema, _search_narratives, _resolve_entity, _get_case, _mo_match,
        _build_network, _geo_query, _trend_series, _build_pack_report,
    ],
    allowed_blocks=frozenset({
        "text", "table", "case_card", "mo_match", "network_graph", "map", "pack_report", "no_answer",
    }),
    prompt_file="report_composer.v1.md",
)

AUDIT_ANALYST = SpecialistConfig(
    name="audit_analyst",
    profile="fast",
    max_iterations=4,
    tools=[_run_sql, _get_schema],
    allowed_blocks=frozenset({"text", "table", "no_answer"}),
    prompt_file="audit_analyst.v1.md",
    sql_scope="audit",
)

REGISTRY: dict[str, SpecialistConfig] = {
    "case_investigator": CASE_INVESTIGATOR,
    "intel_analyst": INTEL_ANALYST,
    "report_composer": REPORT_COMPOSER,
    "audit_analyst": AUDIT_ANALYST,
}


def intel_analyst_scoped_tools() -> list[Tool]:
    """§4.1 rule 4: on station-scoped IO turns, intel_analyst runs WITHOUT
    run_sql/get_schema — closes the raw-SQL bypass around the scope
    injections. build_network/geo_query/trend_series remain (they fully
    cover the three scoped classes per spec, and each enforces its own
    injection/admission check via ctx.scoped_station_id)."""
    return [_resolve_entity, _build_network, _geo_query, _trend_series]
