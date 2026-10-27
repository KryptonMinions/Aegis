"""Standalone HTML -> PDF microservice (headless Chromium via Playwright).

POST /pdf {"html": "..."} -> application/pdf. Kept as its own deployable so the
API tier stays free of the Chromium dependency. Deploy target: Cloud Run
(private), same auth convention as embeddings-service (static bearer key).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import Response
from playwright.async_api import async_playwright
from pydantic import BaseModel

_API_KEY = os.environ.get("PDF_API_KEY", "")
_browser = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser
    async with async_playwright() as pw:
        # --no-sandbox: Cloud Run's gVisor sandbox can't host Chromium's own.
        _browser = await pw.chromium.launch(args=["--no-sandbox"])
        yield
        await _browser.close()


app = FastAPI(title="KSP PDF Service", lifespan=lifespan)


def _require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    if _API_KEY and authorization != f"Bearer {_API_KEY}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


class PdfRequest(BaseModel):
    html: str


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/pdf", dependencies=[Depends(_require_auth)])
async def pdf(body: PdfRequest) -> Response:
    page = await _browser.new_page()
    try:
        await page.set_content(body.html, wait_until="networkidle")
        data = await page.pdf(format="A4", print_background=True)
    finally:
        await page.close()
    return Response(content=data, media_type="application/pdf")
