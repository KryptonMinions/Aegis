"""Standalone embeddings microservice — OpenAI-compatible POST /v1/embeddings
over intfloat/multilingual-e5-base (768-dim), matching scripts/seed/04_embed.py.

Kept as its own deployable (not in-process in the main FastAPI backend) so the
API tier stays free of the torch/transformers dependency and this can scale
independently. Deploy target: Cloud Run (private), same as the main backend.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings, get_settings
from .model import get_embedder

app = FastAPI(title="KSP Embeddings Service")


def _require_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.embeddings_api_key:
        return  # auth disabled — local dev only
    expected = f"Bearer {settings.embeddings_api_key}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


AuthDep = Depends(_require_auth)
SettingsDep = Annotated[Settings, Depends(get_settings)]


class EmbeddingsRequest(BaseModel):
    model: str | None = None
    input: str | list[str] = Field(..., description="Text or list of texts to embed.")


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class Usage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingItem]
    model: str
    usage: Usage


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/embeddings", response_model=EmbeddingsResponse, dependencies=[AuthDep])
async def embeddings(body: EmbeddingsRequest, settings: SettingsDep) -> EmbeddingsResponse:
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts:
        raise HTTPException(status_code=422, detail="input must be non-empty")
    if len(texts) > settings.embed_max_batch:
        raise HTTPException(
            status_code=422,
            detail=f"input batch too large ({len(texts)} > {settings.embed_max_batch})",
        )

    embedder = get_embedder(settings)
    vectors = await run_in_threadpool(embedder.embed, texts)

    approx_tokens = sum(max(1, len(t) // 4) for t in texts)  # rough estimate only
    return EmbeddingsResponse(
        data=[EmbeddingItem(index=i, embedding=v) for i, v in enumerate(vectors)],
        model=settings.embed_model_id,
        usage=Usage(prompt_tokens=approx_tokens, total_tokens=approx_tokens),
    )
