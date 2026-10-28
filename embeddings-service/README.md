# Embeddings Service

Standalone microservice exposing an OpenAI-compatible `POST /v1/embeddings`
over `intfloat/multilingual-e5-base` (768-dim), so the main backend's
`search_narratives` tool (`backend/app/agent/tools/retrieval.py`) can embed a
query at runtime in the exact same vector space as the passages already
seeded into `document_chunks` by `scripts/seed/04_embed.py`.

Kept as its own deployable rather than in-process in the main FastAPI backend:
the backend stays free of the torch/transformers dependency (matches its own
`httpx`-only, thin-app-tier convention — see `ORCHESTRATOR_STEERING.md` O-3),
and this service can scale/redeploy independently. Deploy target: Cloud
Run (private), alongside the main backend.

The Docker image bakes the model weights in at build time, so a cold start loads
them from disk instead of downloading from Hugging Face.

## Run locally

```
cd embeddings-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set EMBEDDINGS_API_KEY to match backend/.env, or leave blank for local dev
uvicorn app.main:app --reload --port 8081
```

First request downloads the model from Hugging Face (~450MB, not gated — no
`HF_TOKEN` needed) and caches it; subsequent requests are fast.

## Contract

`POST /v1/embeddings` — body `{"input": "text" | ["text", ...]}` → OpenAI-shaped
`{"data": [{"index", "embedding"}], "model", "usage"}`.

**No prefix is injected.** The e5 `"query: "` / `"passage: "` convention is the
caller's responsibility (matches `04_embed.py`'s own docstring: "query-time
retrieval code applies 'query: ' on its own side — not this script's
concern"). `search_narratives` must prepend `"query: "` before calling this
endpoint; never re-embed passages here — those are already in `document_chunks`.

Point `backend/.env`'s `EMBEDDINGS_BASE_URL` at this service's `/v1` root
(e.g. `http://localhost:8081/v1`) and `EMBEDDINGS_API_KEY` at the same shared
secret configured here.
