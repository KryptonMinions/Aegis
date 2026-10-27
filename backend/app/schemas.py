from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator

from app.roles import Role


class LoginRequest(BaseModel):
    username: str
    password: str


class SessionUser(BaseModel):
    id: str
    role: Role


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    user: SessionUser


class CurrentUser(BaseModel):
    id: str
    role: Role
    # Officer identity from app_metadata.officer_id (ORCHESTRATOR_STEERING.md
    # O-11) — the only trusted officer-identity source, used for jurisdiction
    # scope derivation and audit. Admin users have none.
    officer_id: str | None = None


class VoiceTranscribeResponse(BaseModel):
    """Sarvam Saaras v3 transcript only -- no translation, no downstream
    processing (VOICE_INTAKE_STEERING.md §3, Phase A)."""

    transcript: str
    detected_language: str | None = None
    request_id: str | None = None


# --------------------------------------------------------------------------
# Ask endpoint (ASK_ENDPOINT_CONTRACT.md §2). Wire types are idiomatic
# snake_case Python/Pydantic -- consistent with every other response in this
# backend (LoginResponse.access_token, VoiceTranscribeResponse.detected_language,
# etc). RealAskService on the frontend translates snake_case wire fields into
# the camelCase shapes frontend/src/lib/types/content-blocks.ts expects, the
# same way auth-api.ts and voice-api.ts already translate their responses.
# --------------------------------------------------------------------------


class Citation(BaseModel):
    level: Literal["fir", "field", "sentence"]
    fir_id: str
    field: str | None = None
    sentence_id: str | None = None
    excerpt: str | None = None


class TableColumn(BaseModel):
    key: str
    label: str
    align: Literal["left", "right"] | None = None


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    id: str
    content: str
    citations: list[Citation] | None = None


class TableBlock(BaseModel):
    type: Literal["table"] = "table"
    id: str
    title: str | None = None
    columns: list[TableColumn]
    rows: list[dict[str, str | int]]
    citations: list[Citation] | None = None


GraphNodeKind = Literal["person", "gang", "phone", "address"]


class GraphNode(BaseModel):
    id: str
    label: str
    kind: GraphNodeKind
    status: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str
    fir_id: str


class NetworkGraphBlock(BaseModel):
    type: Literal["network_graph"] = "network_graph"
    id: str
    central_node_id: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    citations: list[Citation] | None = None


MoOutcome = Literal["convicted", "trial_pending", "investigation_ongoing", "closed_false"]


class MoMatch(BaseModel):
    fir_id: str
    station: str
    similarity: float
    outcome: MoOutcome
    cracked_by: str | None = None


class MoMatchBlock(BaseModel):
    type: Literal["mo_match"] = "mo_match"
    id: str
    query_description: str
    matches: list[MoMatch]
    common_thread: str | None = None
    citations: list[Citation] | None = None


class EntitySummary(BaseModel):
    id: str
    name: str
    kind: Literal["person", "case", "fir", "location", "gang", "phone", "address"]
    subtitle: str | None = None


class CaseSummary(BaseModel):
    fir_id: str
    station: str
    offence: str
    section: str
    status: str
    detail: str | None = None


class HistorySheetSummary(BaseModel):
    id: str
    station: str
    opened_on: str
    category: str
    risk_level: Literal["Low", "Medium", "High"]
    registered_cases: int
    convictions: int
    absconding_instances: int


class CaseCardBlock(BaseModel):
    type: Literal["case_card"] = "case_card"
    id: str
    person: EntitySummary | None = None
    cases: list[CaseSummary]
    history_sheet: HistorySheetSummary | None = None
    citations: list[Citation] | None = None


NoAnswerReason = Literal[
    "not_found", "low_confidence", "out_of_scope", "invalid_reference"
]


class NoAnswerBlock(BaseModel):
    type: Literal["no_answer"] = "no_answer"
    id: str
    message: str
    # Graceful-refusal contract (DEMO_SCENARIOS.md §2.1). Optional for backward
    # compatibility with existing fixtures; the frontend renders unknown/absent
    # reasons as the generic calm block.
    reason: NoAnswerReason | None = None
    citations: list[Citation] | None = None


class TrendMetric(BaseModel):
    category: str
    current: float
    previous: float
    delta_pct: float
    trend: Literal["up", "down", "stable"]
    anomaly: bool | None = None
    anomaly_note: str | None = None
    underlying_fir_ids: list[str] | None = None


class PackReportBlock(BaseModel):
    type: Literal["pack_report"] = "pack_report"
    id: str
    title: str
    period: str
    metrics: list[TrendMetric]
    exportable: bool | None = None
    # Download URL for the rendered PDF (unused so far; /export/pdf is stateless). Set
    # server-side when PDF_EXPORT_ENABLED and the pack is exportable.
    export_url: str | None = None
    citations: list[Citation] | None = None


# MapBlock — field-for-field identical to content-blocks.ts MapBlock (snake_case
# wire; the frontend RealAskService already translates it to camelCase).
class MapCenter(BaseModel):
    lat: float
    lng: float


class MapMarker(BaseModel):
    id: str
    lat: float
    lng: float
    kind: Literal["fir", "station", "hotspot"]
    label: str
    fir_id: str | None = None
    offence: str | None = None
    date: str | None = None
    status: str | None = None


class MapRadius(BaseModel):
    center_lat: float
    center_lng: float
    radius_meters: float
    label: str | None = None


class MapBlock(BaseModel):
    type: Literal["map"] = "map"
    id: str
    title: str | None = None
    center: MapCenter
    zoom: int | None = None
    markers: list[MapMarker]
    radius: MapRadius | None = None
    citations: list[Citation] | None = None


ContentBlock = Annotated[
    Union[
        TextBlock,
        TableBlock,
        NetworkGraphBlock,
        MoMatchBlock,
        CaseCardBlock,
        NoAnswerBlock,
        PackReportBlock,
        MapBlock,
    ],
    Field(discriminator="type"),
]


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    # Not in the ASK_ENDPOINT_CONTRACT.md §2.4 shape as written, but every
    # ported story fixture carries a top-level spoken-language summary
    # alongside its blocks (frontend/src/lib/types/content-blocks.ts
    # ChatMessage.text) -- omitting it would make the real and mock paths
    # render different content, which fails the §5 acceptance criterion.
    text: str | None = None
    blocks: list[ContentBlock] = Field(min_length=1)


class AskRequest(BaseModel):
    query: str
    thread_id: str | None = None
    turn_index: int
    input_modality: Literal["text", "voice"]
    detected_language: str | None = None
    client_ts: str

    @field_validator("query")
    @classmethod
    def _query_length(cls, value: str) -> str:
        trimmed = value.strip()
        if not 1 <= len(trimmed) <= 2000:
            raise ValueError("query must be 1..2000 characters after trimming")
        return trimmed

    @field_validator("turn_index")
    @classmethod
    def _turn_index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("turn_index must be >= 0")
        return value


class AskResponse(BaseModel):
    thread_id: str
    message_id: str
    server_ts: str
    message: AssistantMessage


class ExportTurn(BaseModel):
    """One turn in a PDF export request. Mirrors frontend ChatMessage — the
    frontend sends its own on-screen conversation (2026-07-26 PDF export:
    stateless, no server-side full-history store yet; see
    steering-docs/POST_OVERNIGHT.md §4 for the deferred durable-storage
    alternative)."""

    role: Literal["user", "assistant"]
    timestamp: str
    text: str | None = None
    blocks: list[ContentBlock] | None = None


class ExportPdfRequest(BaseModel):
    thread_id: str | None = None
    turns: list[ExportTurn] = Field(min_length=1)
