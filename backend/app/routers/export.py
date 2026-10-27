import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from app.pdf_export import html_to_pdf, render_session_html
from app.schemas import ExportPdfRequest
from app.security import CurrentUserDep, SettingsDep

router = APIRouter(prefix="/export", tags=["export"])


@router.post("/pdf")
async def export_pdf(
    body: ExportPdfRequest, current_user: CurrentUserDep, settings: SettingsDep
) -> Response:
    """Stateless PDF export: the frontend sends the conversation it already
    has on screen (full blocks + citations); nothing is persisted server-side
    (steering-docs/POST_OVERNIGHT.md §4 tracks durable history as future scope).
    """
    if not settings.pdf_export_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="PDF export is not enabled")

    html = render_session_html(body.turns, role=current_user.role.value, thread_id=body.thread_id)
    try:
        pdf_bytes = await html_to_pdf(html, settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"PDF generation failed: {exc}") from exc

    filename = f"ksp-ask-session-{body.thread_id or 'export'}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
