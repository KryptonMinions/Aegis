"""search_narratives gate.

Mocked: the embeddings client (query prefix, auth header, error handling).
Live (needs Supabase creds in backend/.env): the real tool against
match_document_chunks, using stored chunk vectors as stand-in query embeddings
so no embeddings service is needed. Checks FIR mapping, sentence citations,
and that victim PII chunks never reach a supervisor/analyst turn.

Run: cd backend && THREAD_STORE_BACKEND=memory python -m scripts.test_search_narratives_gate
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import app.agent.tools.retrieval as retrieval  # noqa: E402
import app.embeddings as emb  # noqa: E402
from app.agent.scratchpad import TurnScratchpad  # noqa: E402
from app.agent.tools.base import ToolContext  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import CurrentUser  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


_REAL_CLIENT = httpx.AsyncClient  # captured once: emb.httpx is this same module, which we patch


def _client_factory(handler):
    return lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)


async def test_embeddings_client() -> None:
    print("=== embeddings client (mocked) ===")
    seen: dict = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 768}]})

    s = Settings(_env_file=None, supabase_url="x", supabase_publishable_key="x", supabase_secret_key="x",
                 embeddings_base_url="http://emb.test/", embeddings_api_key="k1", embeddings_model="m")
    emb.httpx.AsyncClient = _client_factory(ok_handler)
    try:
        vec = await emb.embed_query("chain snatching", s)
        check("returns a 768-dim vector", len(vec) == 768)
        check("applies the e5 'query: ' prefix", seen["body"]["input"] == "query: chain snatching", str(seen["body"]))
        check("sends the bearer key", seen["auth"] == "Bearer k1")

        emb.httpx.AsyncClient = _client_factory(lambda r: httpx.Response(500))
        try:
            await emb.embed_query("x", s)
            check("HTTP 500 raises EmbeddingsError", False)
        except emb.EmbeddingsError:
            check("HTTP 500 raises EmbeddingsError", True)

        emb.httpx.AsyncClient = _client_factory(lambda r: httpx.Response(200, json={"data": [{"embedding": [0.1] * 10}]}))
        try:
            await emb.embed_query("x", s)
            check("wrong dimension raises EmbeddingsError", False)
        except emb.EmbeddingsError:
            check("wrong dimension raises EmbeddingsError", True)
    finally:
        emb.httpx.AsyncClient = _REAL_CLIENT
    try:
        await emb.embed_query("x", Settings(_env_file=None, supabase_url="x", supabase_publishable_key="x", supabase_secret_key="x"))
        check("missing EMBEDDINGS_BASE_URL raises EmbeddingsError", False)
    except emb.EmbeddingsError:
        check("missing EMBEDDINGS_BASE_URL raises EmbeddingsError", True)


async def _stored_vector(settings: Settings, **eq: str) -> tuple[list[float], dict] | None:
    qs = "&".join(f"{k}=eq.{v}" for k, v in eq.items())
    r = httpx.get(
        f"{settings.supabase_url}/rest/v1/document_chunks?select=chunk_id,source_table,source_id,source_field,embedding&{qs}&embedding=not.is.null&limit=1",
        headers={"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"},
        timeout=30,
    )
    rows = r.json()
    if not rows:
        return None
    return json.loads(rows[0]["embedding"]), rows[0]


async def test_live() -> None:
    print("\n=== live tool against Supabase ===")
    settings = get_settings()
    tool = retrieval.SearchNarrativesTool()

    async def run(role: Role, vector: list[float], **args):
        async def fake_embed(text, s):
            return vector
        retrieval.embed_query = fake_embed
        pad = TurnScratchpad()
        ctx = ToolContext(user=CurrentUser(id="u", role=role, officer_id=None), frame=None, settings=settings, scratchpad=pad)
        return await tool.run({"query_text": "q", **args}, ctx), pad

    narrative = await _stored_vector(settings, source_table="firs", source_field="fir_narrative")
    diary = await _stored_vector(settings, source_table="case_diary_entries")
    victim = await _stored_vector(settings, source_table="fir_victims", source_field="victim_statement")
    check("found sample chunks to query with", all([narrative, diary, victim]))
    if not all([narrative, diary, victim]):
        return

    res, pad = await run(Role.INVESTIGATING_OFFICER, narrative[0], top_k=3)
    top = res.data["chunks"][0] if res.ok and res.data["chunks"] else {}
    check("FIR narrative query finds that FIR first", top.get("fir_id") == narrative[1]["source_id"], str(top)[:120])
    check("similarity is ~1 for an identical vector", top.get("similarity", 0) > 0.99, str(top.get("similarity")))
    check("every chunk has a citation key", all(c["citation_key"] for c in res.data["chunks"]))
    entry = pad.provenance.get(top.get("citation_key", ""))
    check("citation is sentence-level with an excerpt", entry is not None and entry.to_citation().level == "sentence" and bool(entry.to_citation().excerpt))
    check("at most top_k chunks returned", res.data["count"] <= 3)

    res, _ = await run(Role.INVESTIGATING_OFFICER, diary[0], top_k=3)
    mapped = res.data["chunks"][0] if res.ok and res.data["chunks"] else {}
    check("diary entry chunk is mapped back to a real FIR id", str(mapped.get("fir_id", "")).startswith("KA-"), str(mapped)[:120])

    res, _ = await run(Role.INVESTIGATING_OFFICER, victim[0], top_k=8)
    check("IO can see victim statements", any(c["source_field"] == "victim_statement" for c in res.data.get("chunks", [])))
    for role in (Role.SUPERVISOR, Role.ANALYST):
        res, _ = await run(role, victim[0], top_k=8)
        fields = {c["source_field"] for c in res.data.get("chunks", [])}
        check(f"{role.value} never receives victim statements or complaint text",
              res.ok and not (fields & {"victim_statement", "complaint_text"}), str(fields))

    res, _ = await run(Role.ANALYST, narrative[0], filters={"fir_id": "bad id'; drop"})
    check("malformed fir_id filter is rejected", not res.ok)
    res, _ = await run(Role.ANALYST, narrative[0], filters={"fir_id": narrative[1]["source_id"]}, top_k=5)
    check("fir_id filter limits results to that FIR", res.ok and all(c["fir_id"] == narrative[1]["source_id"] for c in res.data["chunks"]) and res.data["count"] >= 1)


async def main() -> int:
    await test_embeddings_client()
    await test_live()
    print(f"\nSEARCH NARRATIVES GATE: {passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
