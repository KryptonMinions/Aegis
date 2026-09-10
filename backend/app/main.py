import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import ask, auth, export, voice

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # R2_steering_docs.md R-1/§2.4: the in-memory ThreadStore is only safe
    # under a single worker. WEB_CONCURRENCY is the standard uvicorn/gunicorn
    # env for worker count; the run/deploy script must set it for this check
    # to catch a real misconfiguration.
    worker_count = int(os.environ.get("WEB_CONCURRENCY", "1") or "1")
    if settings.thread_store_backend == "memory" and worker_count > 1:
        message = (
            f"THREAD_STORE_BACKEND=memory is not safe with WEB_CONCURRENCY="
            f"{worker_count} (in-process store, no cross-worker sharing). Set "
            "THREAD_STORE_BACKEND=catalyst or run with a single worker."
        )
        logger.fatal(message)
        raise RuntimeError(message)
    yield


app = FastAPI(title="KSP Datathon Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(voice.router)
app.include_router(ask.router)
app.include_router(export.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
