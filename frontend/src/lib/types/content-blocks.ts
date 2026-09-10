import type { CaseSummary, EntitySummary, HistorySheetSummary } from "./domain";

/**
 * Citations are always-on (never a toggle) at one of three levels —
 * FIR, field, or sentence — per steering-docs/FRONTEND_UI_STEERING.md §5.
 */
export type CitationLevel = "fir" | "field" | "sentence";

export interface Citation {
  level: CitationLevel;
  firId: string;
  field?: string;
  sentenceRef?: string;
  label: string;
}

interface BaseBlock {
  id: string;
  citations?: Citation[];
}

export interface TextBlock extends BaseBlock {
  type: "text";
  content: string;
}

export interface TableColumn {
  key: string;
  label: string;
  align?: "left" | "right";
}

export interface TableBlock extends BaseBlock {
  type: "table";
  title?: string;
  columns: TableColumn[];
  rows: Record<string, string | number>[];
}

export type GraphNodeKind = "person" | "gang" | "phone" | "address";

export interface GraphNode {
  id: string;
  label: string;
  kind: GraphNodeKind;
  status?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  label: string;
  firId: string;
}

export interface NetworkGraphBlock extends BaseBlock {
  type: "network_graph";
  centralNodeId: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export type MoOutcome =
  | "convicted"
  | "trial_pending"
  | "investigation_ongoing"
  | "closed_false";

export interface MoMatch {
  firId: string;
  station: string;
  similarity: number;
  outcome: MoOutcome;
  crackedBy?: string;
}

export interface MoMatchBlock extends BaseBlock {
  type: "mo_match";
  queryDescription: string;
  matches: MoMatch[];
  commonThread?: string;
}

export interface CaseCardBlock extends BaseBlock {
  type: "case_card";
  person?: EntitySummary;
  cases: CaseSummary[];
  historySheet?: HistorySheetSummary;
}

export type NoAnswerReason = "not_found" | "low_confidence" | "out_of_scope" | "invalid_reference";

export interface NoAnswerBlock extends BaseBlock {
  type: "no_answer";
  message: string;
  /** Optional for backward compat with existing fixtures; unknown/absent
   * values render as the generic calm block (steering-docs §13.2). */
  reason?: NoAnswerReason;
}

export interface TrendMetric {
  category: string;
  current: number;
  previous: number;
  deltaPct: number;
  trend: "up" | "down" | "stable";
  anomaly?: boolean;
  anomalyNote?: string;
  underlyingFirIds?: string[];
}

export interface PackReportBlock extends BaseBlock {
  type: "pack_report";
  title: string;
  period: string;
  metrics: TrendMetric[];
  exportable?: boolean;
}

export type MapMarkerKind = "fir" | "station" | "hotspot";

export interface MapMarker {
  id: string;
  lat: number;
  lng: number;
  kind: MapMarkerKind;
  label: string;
  firId?: string;
  offence?: string;
  date?: string;
  status?: string;
}

/** Optional radius overlay, e.g. the 500 m ring around a police station. */
export interface MapRadius {
  centerLat: number;
  centerLng: number;
  radiusMeters: number;
  label?: string;
}

export interface MapBlock extends BaseBlock {
  type: "map";
  title?: string;
  center: { lat: number; lng: number };
  zoom?: number;
  markers: MapMarker[];
  radius?: MapRadius;
}

export type ContentBlock =
  | TextBlock
  | TableBlock
  | NetworkGraphBlock
  | MoMatchBlock
  | CaseCardBlock
  | NoAnswerBlock
  | PackReportBlock
  | MapBlock;

/** One entry in the live "thinking" activity trace streamed over
 * `/ask/stream` (ASK_STREAM_ENDPOINT_CONTRACT.md §2.2) — the agent's own
 * tool-use trail (working notes + tool-call lifecycle), not raw model
 * chain-of-thought. `tool_started`/`tool_finished` are separate events for
 * the same call, correlated by `tool` + arrival order, not by `label`. */
export interface ThinkingEvent {
  id: string;
  kind:
    | "thought"
    | "cache_hit"
    | "tool_started"
    | "tool_finished"
    | "hard_refuse"
    | "llm_error"
    | "abort"
    | "final_answer_parse_failed";
  label: string;
  detail?: string;
  status: "in_progress" | "done" | "error";
  ts: string;
  tool?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  timestamp: string;
  text?: string;
  blocks?: ContentBlock[];
  /** Server-assigned thread id (ASK_ENDPOINT_CONTRACT.md §2.4) — the chat
   * container carries this forward as `threadId` on the next AskInput so
   * later turns land in the same thread. Undefined for user messages and
   * for MockAskService responses (no server-side thread concept). */
  threadId?: string;
  /** Live activity trace while this message is streaming, and its collapsed
   * history afterward. Undefined for user messages, MockAskService
   * responses, and any answer served by the non-streaming `/ask` path. */
  thinking?: ThinkingEvent[];
  /** True from the moment a placeholder assistant message is appended until
   * its terminal `message`/`error` SSE event arrives (ChatThread.tsx). */
  isStreaming?: boolean;
}
