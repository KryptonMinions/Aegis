"""Client for the embeddings-service (OpenAI-compatible POST /v1/embeddings).

Plain httpx, same convention as app/supabase.py and app/llm/client.py. The e5
"query: " prefix is applied here, because the service embeds exactly the text it
is given and the seeded passages were embedded with "passage: ".
"""

from __future__ import annotations

import httpx

from app.config import Settings

EMBED_DIM = 768  # multilingual-e5-base; must match document_chunks.embedding

# The service scales to zero, so the first call after idle waits for a cold
# start (container + model load). Stay under the search tool's own timeout.
EMBED_TIMEOUT_S = 28


class EmbeddingsError(Exception):
    pass


async def embed_query(text: str, settings: Settings) -> list[float]:
    if not settings.embeddings_base_url:
        raise EmbeddingsError("EMBEDDINGS_BASE_URL is not configured")
    headers = {"Content-Type": "application/json"}
    if settings.embeddings_api_key:
        headers["Authorization"] = f"Bearer {settings.embeddings_api_key}"
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as client:
            resp = await client.post(
                f"{settings.embeddings_base_url.rstrip('/')}/v1/embeddings",
                json={"model": settings.embeddings_model, "input": f"query: {text}"},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise EmbeddingsError(f"embeddings service unreachable ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise EmbeddingsError(f"embeddings service returned {resp.status_code}")
    try:
        vector = resp.json()["data"][0]["embedding"]
    except (KeyError, IndexError, ValueError) as exc:
        raise EmbeddingsError("unexpected embeddings response shape") from exc
    if len(vector) != EMBED_DIM:
        raise EmbeddingsError(f"expected a {EMBED_DIM}-dim vector, got {len(vector)}")
    return vector
