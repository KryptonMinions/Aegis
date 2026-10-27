"""Session -> printable HTML (app/routers/export.py -> pdf-service).

Renders a full chat session (turns + all content-block types + citations) as
clean HTML for PDF conversion. Interactive blocks (network_graph, map) get a
static table approximation — a PDF can't host live interactivity.
"""

from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone

import httpx
import markdown as md

from app.config import Settings
from app.schemas import (
    CaseCardBlock,
    Citation,
    ContentBlock,
    ExportTurn,
    MapBlock,
    MoMatchBlock,
    NetworkGraphBlock,
    NoAnswerBlock,
    PackReportBlock,
    TableBlock,
    TextBlock,
)


def _esc(value: str | None) -> str:
    return html_lib.escape(value or "")


def _render_citations(citations: list[Citation] | None) -> str:
    if not citations:
        return ""
    items = []
    for c in citations:
        label = f"FIR {_esc(c.fir_id)}"
        if c.field:
            label += f" — {_esc(c.field)}"
        if c.excerpt:
            label += f': "{_esc(c.excerpt)}"'
        items.append(f"<li>{label}</li>")
    return f'<div class="citations"><span class="citations-label">Sources:</span><ul>{"".join(items)}</ul></div>'


def _render_text(block: TextBlock) -> str:
    body = md.markdown(block.content, extensions=["tables"])
    return f'<div class="block block-text">{body}{_render_citations(block.citations)}</div>'


def _render_table(block: TableBlock) -> str:
    head = "".join(f"<th>{_esc(c.label)}</th>" for c in block.columns)
    rows_html = [
        "<tr>" + "".join(f"<td>{_esc(str(row.get(c.key, '')))}</td>" for c in block.columns) + "</tr>"
        for row in block.rows
    ]
    title_html = f"<h4>{_esc(block.title)}</h4>" if block.title else ""
    return (
        f'<div class="block block-table">{title_html}'
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
        f"{_render_citations(block.citations)}</div>"
    )


def _render_case_card(block: CaseCardBlock) -> str:
    parts = ['<div class="block block-case-card">']
    if block.person:
        parts.append(f"<h4>{_esc(block.person.name)}</h4>")
        if block.person.subtitle:
            parts.append(f'<p class="muted">{_esc(block.person.subtitle)}</p>')
    if block.history_sheet:
        hs = block.history_sheet
        parts.append(
            "<table><tbody>"
            f"<tr><td>History Sheet</td><td>{_esc(hs.id)}</td></tr>"
            f"<tr><td>Station</td><td>{_esc(hs.station)}</td></tr>"
            f"<tr><td>Category</td><td>{_esc(hs.category)}</td></tr>"
            f"<tr><td>Risk level</td><td>{_esc(hs.risk_level)}</td></tr>"
            f"<tr><td>Registered cases</td><td>{hs.registered_cases}</td></tr>"
            f"<tr><td>Convictions</td><td>{hs.convictions}</td></tr>"
            f"<tr><td>Absconding instances</td><td>{hs.absconding_instances}</td></tr>"
            "</tbody></table>"
        )
    if block.cases:
        rows = "".join(
            f"<tr><td>{_esc(c.fir_id)}</td><td>{_esc(c.station)}</td><td>{_esc(c.offence)}</td>"
            f"<td>{_esc(c.section)}</td><td>{_esc(c.status)}</td></tr>"
            for c in block.cases
        )
        parts.append(
            "<table><thead><tr><th>FIR</th><th>Station</th><th>Offence</th>"
            f"<th>Section</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    parts.append(_render_citations(block.citations))
    parts.append("</div>")
    return "".join(parts)


def _render_mo_match(block: MoMatchBlock) -> str:
    rows = "".join(
        f"<tr><td>{_esc(m.fir_id)}</td><td>{_esc(m.station)}</td>"
        f"<td>{m.similarity:.2f}</td><td>{_esc(m.outcome)}</td></tr>"
        for m in block.matches
    )
    common = f"<p><em>{_esc(block.common_thread)}</em></p>" if block.common_thread else ""
    return (
        f'<div class="block block-mo-match"><h4>{_esc(block.query_description)}</h4>{common}'
        "<table><thead><tr><th>FIR</th><th>Station</th><th>Similarity</th><th>Outcome</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{_render_citations(block.citations)}</div>"
    )


def _render_network_graph(block: NetworkGraphBlock) -> str:
    node_rows = "".join(
        f"<tr><td>{_esc(n.label)}</td><td>{_esc(n.kind)}</td><td>{_esc(n.status or '')}</td></tr>"
        for n in block.nodes
    )
    edge_rows = "".join(
        f"<tr><td>{_esc(e.source)}</td><td>{_esc(e.label)}</td><td>{_esc(e.target)}</td><td>{_esc(e.fir_id)}</td></tr>"
        for e in block.edges
    )
    return (
        '<div class="block block-network"><p class="muted">Network graph (static summary — '
        "interactive view available in the app)</p>"
        "<table><thead><tr><th>Entity</th><th>Kind</th><th>Status</th></tr></thead>"
        f"<tbody>{node_rows}</tbody></table>"
        "<table><thead><tr><th>From</th><th>Relationship</th><th>To</th><th>FIR</th></tr></thead>"
        f"<tbody>{edge_rows}</tbody></table>{_render_citations(block.citations)}</div>"
    )


def _render_map(block: MapBlock) -> str:
    rows = "".join(
        f"<tr><td>{_esc(m.label)}</td><td>{_esc(m.kind)}</td><td>{m.lat:.5f}, {m.lng:.5f}</td>"
        f"<td>{_esc(m.fir_id or '')}</td><td>{_esc(m.status or '')}</td></tr>"
        for m in block.markers
    )
    title_html = f"<h4>{_esc(block.title)}</h4>" if block.title else ""
    radius_html = f'<p class="muted">{_esc(block.radius.label)}</p>' if block.radius and block.radius.label else ""
    return (
        f'<div class="block block-map">{title_html}<p class="muted">Map (static summary — '
        "interactive view available in the app)</p>"
        "<table><thead><tr><th>Location</th><th>Kind</th><th>Coordinates</th><th>FIR</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{radius_html}{_render_citations(block.citations)}</div>"
    )


def _render_pack_report(block: PackReportBlock) -> str:
    rows = []
    for m in block.metrics:
        anomaly = ' <span class="anomaly">⚠</span>' if m.anomaly else ""
        rows.append(
            f"<tr><td>{_esc(m.category)}</td><td>{m.current:g}</td><td>{m.previous:g}</td>"
            f"<td>{m.delta_pct:+.1f}%</td><td>{_esc(m.trend)}{anomaly}</td></tr>"
        )
    return (
        f'<div class="block block-pack"><h4>{_esc(block.title)}</h4><p class="muted">{_esc(block.period)}</p>'
        "<table><thead><tr><th>Category</th><th>Current</th><th>Previous</th>"
        f"<th>Change</th><th>Trend</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        f"{_render_citations(block.citations)}</div>"
    )


def _render_no_answer(block: NoAnswerBlock) -> str:
    return f'<div class="block block-no-answer">{_esc(block.message)}</div>'


_RENDERERS = {
    "text": _render_text,
    "table": _render_table,
    "case_card": _render_case_card,
    "mo_match": _render_mo_match,
    "network_graph": _render_network_graph,
    "map": _render_map,
    "pack_report": _render_pack_report,
    "no_answer": _render_no_answer,
}


def _render_block(block: ContentBlock) -> str:
    renderer = _RENDERERS.get(block.type)
    return renderer(block) if renderer else ""


def render_session_html(
    turns: list[ExportTurn],
    *,
    role: str,
    thread_id: str | None,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    turn_html: list[str] = []
    for turn in turns:
        if turn.role == "user":
            turn_html.append(f'<div class="turn turn-user"><p>{_esc(turn.text)}</p></div>')
        else:
            blocks_html = "".join(_render_block(b) for b in (turn.blocks or []))
            turn_html.append(f'<div class="turn turn-assistant">{blocks_html}</div>')

    meta_line = _esc(role.replace("_", " ").title())
    thread_line = f" &middot; Thread {_esc(thread_id)}" if thread_id else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; color: #1a1a1a; font-size: 12px; margin: 32px; }}
  .header {{ border-bottom: 2px solid #0a3d62; padding-bottom: 12px; margin-bottom: 20px; }}
  .header h1 {{ font-size: 18px; margin: 0 0 4px 0; color: #0a3d62; }}
  .header .meta {{ color: #555; font-size: 11px; }}
  .turn {{ margin-bottom: 16px; page-break-inside: avoid; }}
  .turn-user p {{ font-weight: 600; background: #f0f4f8; padding: 8px 12px; border-radius: 6px; margin: 0; }}
  .turn-user p::before {{ content: "Q: "; color: #0a3d62; }}
  .block {{ margin: 8px 0 8px 12px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 6px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; font-size: 11px; }}
  th {{ background: #f0f4f8; }}
  .muted {{ color: #777; font-size: 10.5px; margin: 2px 0; }}
  .citations {{ font-size: 10px; color: #666; margin-top: 4px; }}
  .citations ul {{ margin: 2px 0 0 0; padding-left: 16px; }}
  .anomaly {{ color: #b34700; }}
  .block-no-answer {{ font-style: italic; color: #666; }}
</style>
</head>
<body>
  <div class="header">
    <h1>KSP Ask &mdash; Session Export</h1>
    <div class="meta">{meta_line}{thread_line} &middot; Generated {generated_at}</div>
  </div>
  {"".join(turn_html)}
</body>
</html>"""


async def html_to_pdf(html: str, settings: Settings) -> bytes:
    """POST the HTML to the Chromium pdf-service and return the PDF bytes."""
    headers = {"Authorization": f"Bearer {settings.pdf_service_api_key}"} if settings.pdf_service_api_key else {}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.pdf_service_base_url.rstrip('/')}/pdf", json={"html": html}, headers=headers
        )
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF-"):
        raise ValueError("pdf-service returned a non-PDF body")
    return resp.content
