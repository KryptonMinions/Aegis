from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_publishable_key: str
    supabase_secret_key: str
    synthetic_email_domain: str = "ksp.internal"
    # Comma-separated list of allowed CORS origins (e.g. local dev + a
    # deployed Vercel frontend at once). No wildcards — every origin the
    # frontend can run from must be listed explicitly.
    frontend_origin: str = "http://localhost:3000"

    @property
    def frontend_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origin.split(",") if o.strip()]

    # Sarvam AI Saaras v3 STT (VOICE_INTAKE_STEERING.md §7). Backend-only secret.
    sarvam_api_key: str = ""
    voice_intake_enabled: bool = True

    # Demo mode: POST /auth/demo-login signs in as the seeded demo user for a
    # role with no password. Anyone who can reach the API can pick any role, so
    # enable it only where all the data is synthetic.
    passwordless_login_enabled: bool = False

    # Per-client-IP rate limits (0 disables a limit). Counters are
    # per-instance, so the effective cap is x Cloud Run instances.
    ask_rate_limit_per_minute: int = 10
    ask_rate_limit_per_hour: int = 100
    login_rate_limit_per_minute: int = 20

    # --- Orchestrator / agent engine (ORCHESTRATOR_STEERING.md §2.1) -------
    # fixture = current dispatch shell; agent = the tool-calling orchestrator.
    ask_engine: Literal["fixture", "agent"] = "fixture"
    ask_max_turn_seconds: int = 45
    ask_token_budget: int = 60000
    ask_tool_timeout_s: int = 10
    ask_sql_row_cap: int = 200
    ask_sql_statement_timeout_ms: int = 8000
    ask_thread_ttl_s: int = 3600
    # R2_steering_docs.md R-2. Off by default (R-5). No request-level effect on
    # the current OpenAI-compat Gemini transport (implicit caching is automatic
    # server-side); gates cache-token instrumentation into ask_turn_traces and
    # reserves the seam for an explicit-cache transport later.
    prompt_cache_enabled: bool = False
    # R2_steering_docs.md R-3 pre-loop semantic answer cache. Off by default
    # (R-5). In-process TTL cache; classes not covered by a TTL below
    # (summary/audit/unresolved) are never cached.
    answer_cache_enabled: bool = False
    answer_cache_ttl_lookup_s: int = 1800
    answer_cache_ttl_analytic_s: int = 300
    # R2_steering_docs.md R-4. Run independent tool calls in one iteration
    # concurrently. run_sql calls stay serialized (executor budgets). Off by
    # default (R-5); flag off = byte-identical sequential execution.
    parallel_tools_enabled: bool = False
    parallel_tools_max: int = 4
    # thinking_state.md §2.5. Off by default; when false, `phase`/`agent_selected`
    # thinking events are never emitted and existing behavior is unchanged.
    ask_stream_phase_events: bool = False
    # ISO date (YYYY-MM-DD); when set, overrides "today" for relative-date
    # resolution and prompts. Align with SEED_DEMO_DATE.
    ask_reference_date: str = ""

    # --- LLM profiles (SEMANTIC_LAYER.md Part 1; ORCHESTRATOR_STEERING.md O-9)
    # OpenAI-compatible endpoints. fast = gemini-2.5-flash, smart =
    # gemini-2.5-pro via Google's OpenAI-compat base URL. Model IDs are env
    # values only, never hardcoded.
    llm_profile_fast_base_url: str = ""
    llm_profile_fast_model: str = ""
    llm_profile_fast_api_key: str = ""
    llm_profile_smart_base_url: str = ""
    llm_profile_smart_model: str = ""
    llm_profile_smart_api_key: str = ""
    # Optional third profile for the single non-tool-calling semantic-layer
    # intent call (any OpenAI-compatible endpoint). Falls back to the fast
    # profile when unset or on transport/output error.
    llm_profile_intent_base_url: str = ""
    llm_profile_intent_model: str = ""
    llm_profile_intent_api_key: str = ""
    llm_max_retries: int = 2
    llm_request_timeout_s: int = 30

    # --- Embeddings (multilingual-e5-base, 768-dim; must match seed 04_embed)
    embeddings_base_url: str = ""
    embeddings_model: str = ""
    embeddings_api_key: str = ""

    # --- GCP integration (each optional) -----------------------------------
    # Thread/session store backend. memory = in-process (single-worker);
    # firestore = Cloud Firestore (durable, multi-instance safe).
    thread_store_backend: Literal["memory", "firestore"] = "memory"
    pdf_export_enabled: bool = False
    # Chromium HTML->PDF microservice (pdf-service/), e.g. its Cloud Run URL.
    pdf_service_base_url: str = ""
    pdf_service_api_key: str = ""
    # Empty = infer from the runtime's application-default credentials.
    gcp_project_id: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
