import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, ChevronDown, Loader2, Square } from "lucide-react";
import type { ThinkingEvent } from "@/lib/types/content-blocks";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/shared/ErrorState";
import { cn } from "@/lib/utils";

// thinking_state.md §10 -- v1 (this file's original behavior) stays the
// default; v2 is opt-in until the flag flips.
const THINKING_UI_V2 = process.env.NEXT_PUBLIC_THINKING_UI === "v2";

const KNOWN_KINDS = new Set<ThinkingEvent["kind"]>([
  "thought",
  "cache_hit",
  "tool_started",
  "tool_finished",
  "hard_refuse",
  "llm_error",
  "abort",
  "final_answer_parse_failed",
  "phase",
  "agent_selected",
]);

// thinking_state.md §4.1
const PHASE_LABELS: Record<string, string> = {
  understanding: "Understanding the question",
  routing: "Selecting specialist",
  working: "Working",
  composing: "Composing the answer",
};

// thinking_state.md §4.2
const TOOL_LABELS: Record<string, string> = {
  run_sql: "Querying records",
  get_schema: "Reading schema",
  search_narratives: "Searching narratives",
  resolve_entity: "Resolving entity",
  get_case: "Retrieving case file",
  mo_match: "Matching modus operandi",
  build_network: "Building association network",
  geo_query: "Mapping locations",
  trend_series: "Computing trends",
};

// thinking_state.md §4.3
const AGENT_LABELS: Record<string, string> = {
  case_investigator: "Case Investigator",
  intel_analyst: "Intelligence Analyst",
  report_composer: "Report Composer",
  audit_analyst: "Audit Analyst",
};

const ROW_CAP = 12;

/**
 * `tool_started`/`tool_finished` are two separate raw events for the same
 * call (ASK_STREAM_ENDPOINT_CONTRACT.md §2.2) -- merge each finished event
 * into its matching started row. Paired by `call_id` when both events carry
 * one (thinking_state.md §7); falls back to FIFO-by-`tool` name otherwise.
 * Rows for `phase`/`agent_selected` and any unrecognized `kind` are dropped
 * (they drive the headline instead, or are ignored -- thinking_state.md §10).
 */
function buildThinkingRows(events: ThinkingEvent[]): ThinkingEvent[] {
  const rows: ThinkingEvent[] = [];
  const openIndexByCallId = new Map<string, number>();
  const openIndexQueues = new Map<string, number[]>();

  for (const evt of events) {
    if (!KNOWN_KINDS.has(evt.kind)) continue;
    if (evt.kind === "phase" || evt.kind === "agent_selected") continue;

    if (evt.kind === "tool_started" && evt.tool) {
      rows.push(evt);
      const idx = rows.length - 1;
      if (evt.call_id) {
        openIndexByCallId.set(evt.call_id, idx);
      } else {
        const queue = openIndexQueues.get(evt.tool) ?? [];
        queue.push(idx);
        openIndexQueues.set(evt.tool, queue);
      }
    } else if (evt.kind === "tool_finished" && evt.tool) {
      let idx = evt.call_id ? openIndexByCallId.get(evt.call_id) : undefined;
      if (idx === undefined) {
        const queue = openIndexQueues.get(evt.tool);
        idx = queue?.shift();
      }
      if (idx !== undefined) {
        rows[idx] = { ...rows[idx], status: evt.status, detail: evt.detail ?? rows[idx].detail, ts: evt.ts, row_count: evt.row_count, latency_ms: evt.latency_ms };
      } else {
        rows.push(evt);
      }
    } else {
      rows.push(evt);
    }
  }
  return rows;
}

function toolLabel(row: ThinkingEvent): string {
  if (row.tool && TOOL_LABELS[row.tool]) return TOOL_LABELS[row.tool];
  return row.label;
}

function StatusIcon({ status }: { status: ThinkingEvent["status"] }) {
  if (status === "in_progress") return <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />;
  if (status === "error") return <AlertCircle className="size-3.5 shrink-0 text-destructive" />;
  return <Check className="size-3.5 shrink-0 text-muted-foreground" />;
}

function rowDetail(row: ThinkingEvent, isFixture: boolean): string | undefined {
  if (row.kind === "tool_finished" || (row.kind === "tool_started" && row.status !== "in_progress")) {
    const parts: string[] = [];
    if (row.detail) parts.push(row.detail);
    if (typeof row.row_count === "number") parts.push(`${row.row_count} rows`);
    if (!isFixture && typeof row.latency_ms === "number") parts.push(`${row.latency_ms}ms`);
    return parts.length > 0 ? parts.join(" · ") : undefined;
  }
  return row.detail;
}

function ThinkingRow({ row, isStreaming, isFixture }: { row: ThinkingEvent; isStreaming?: boolean; isFixture: boolean }) {
  // `thought` events (unlike tool_started/tool_finished pairs) never get a
  // terminal status from the backend -- once the turn itself is done, a
  // still-"in_progress" row means "resolved", not "still spinning".
  const status = !isStreaming && row.status === "in_progress" ? "done" : row.status;
  const detail = rowDetail(row, isFixture);
  return (
    <div className="flex items-start gap-2 text-xs text-muted-foreground">
      <span className="mt-0.5">
        <StatusIcon status={status} />
      </span>
      <span className="line-clamp-1">
        <span className="text-foreground">{toolLabel(row)}</span>
        {detail && <span className="ml-1">— {detail}</span>}
      </span>
    </div>
  );
}

function useElapsedSeconds(startedAt?: number, endedAt?: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt || endedAt) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [startedAt, endedAt]);
  if (!startedAt) return 0;
  return Math.floor(((endedAt ?? now) - startedAt) / 1000);
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function headlineFor(
  status: ThinkingPanelProps["status"],
  currentPhase: ThinkingPanelProps["currentPhase"],
  currentAgent: ThinkingPanelProps["currentAgent"],
  elapsedSeconds: number,
): string {
  if ((status === "submitting" || status === "waiting") && elapsedSeconds < 2) return "Working";
  if (status === "waiting") return "Contacting the assistant";
  if (currentPhase === "working" && currentAgent && AGENT_LABELS[currentAgent]) {
    return `Working — ${AGENT_LABELS[currentAgent]}`;
  }
  if (currentPhase && PHASE_LABELS[currentPhase]) return PHASE_LABELS[currentPhase];
  return "Working";
}

interface ThinkingPanelProps {
  thinking?: ThinkingEvent[];
  isStreaming?: boolean;
  status?: "submitting" | "waiting" | "streaming" | "composing" | "done" | "error" | "aborted";
  startedAt?: number;
  endedAt?: number | null;
  currentPhase?: "understanding" | "routing" | "working" | "composing" | null;
  currentAgent?: string | null;
  onStop?: () => void;
  errorMessage?: string;
  onRetry?: () => void;
}

/** Live activity trace while streaming; collapses to a one-line disclosure
 * once the final answer has landed -- the familiar Claude/ChatGPT/Gemini
 * "thinking" pattern, sourced from the agent's own tool-use trail rather
 * than provider chain-of-thought (ASK_STREAM_ENDPOINT_CONTRACT.md §1.1). */
export function ThinkingPanel(props: ThinkingPanelProps) {
  if (THINKING_UI_V2) return <ThinkingPanelV2 {...props} />;
  return <ThinkingPanelV1 thinking={props.thinking} isStreaming={props.isStreaming} />;
}

function ThinkingPanelV1({ thinking, isStreaming }: { thinking?: ThinkingEvent[]; isStreaming?: boolean }) {
  const rows = useMemo(() => buildThinkingRows(thinking ?? []), [thinking]);
  if (rows.length === 0) return null;

  if (isStreaming) {
    return (
      <div className="space-y-1.5">
        {rows.map((row) => (
          <ThinkingRow key={row.id} row={row} isStreaming={isStreaming} isFixture={false} />
        ))}
      </div>
    );
  }

  return (
    <Collapsible className="rounded-md border border-border/60">
      <CollapsibleTrigger className="group/thinking-trigger flex items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground">
        <ChevronDown className="size-3.5 shrink-0 transition-transform group-aria-expanded/thinking-trigger:rotate-180" />
        Worked through {rows.length} step{rows.length === 1 ? "" : "s"}
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-1.5 px-3 pb-2">
        {rows.map((row) => (
          <ThinkingRow key={row.id} row={row} isStreaming={isStreaming} isFixture={false} />
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}

function ThinkingPanelV2({
  thinking,
  status,
  startedAt,
  endedAt,
  currentPhase,
  currentAgent,
  onStop,
  errorMessage,
  onRetry,
}: ThinkingPanelProps) {
  const allRows = useMemo(() => buildThinkingRows(thinking ?? []), [thinking]);
  const isFixture = useMemo(() => (thinking ?? []).some((e) => e.fixture), [thinking]);
  const [expanded, setExpanded] = useState(false);
  const [collapsibleOpen, setCollapsibleOpen] = useState(false);
  const elapsedSeconds = useElapsedSeconds(startedAt, endedAt);

  const isTerminal = status === "done" || status === "error" || status === "aborted";
  const isActive = !isTerminal;

  if (!status && allRows.length === 0) return null;

  const rows = expanded || allRows.length <= ROW_CAP ? allRows : allRows.slice(allRows.length - ROW_CAP);
  const collapsedCount = allRows.length - rows.length;

  const headline = headlineFor(status, currentPhase, currentAgent, elapsedSeconds);
  const stallLine =
    isActive && status === "streaming" && elapsedSeconds >= 20
      ? "Taking longer than usual"
      : isActive && status === "streaming" && elapsedSeconds >= 8
        ? "Still working"
        : null;

  const collapsedSummary = isFixture
    ? `Demo response · ${allRows.length} step${allRows.length === 1 ? "" : "s"}`
    : `Reasoned for ${formatElapsed(elapsedSeconds)} · ${allRows.length} step${allRows.length === 1 ? "" : "s"}`;

  if (status === "done" && allRows.length > 0) {
    return (
      <Collapsible
        className="rounded-md border border-border/60"
        open={collapsibleOpen}
        onOpenChange={setCollapsibleOpen}
      >
        <CollapsibleTrigger
          aria-label={collapsibleOpen ? "Hide reasoning steps" : "Show reasoning steps"}
          className="group/thinking-trigger flex items-center gap-1.5 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <ChevronDown className="size-3.5 shrink-0 transition-transform group-aria-expanded/thinking-trigger:rotate-180" />
          {collapsedSummary}
          {isFixture && (
            <span className="ml-1 rounded-sm bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              Demo data
            </span>
          )}
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-1.5 px-3 pb-2">
          <RowList rows={rows} collapsedCount={collapsedCount} isFixture={isFixture} isStreaming={false} onExpand={() => setExpanded(true)} />
        </CollapsibleContent>
      </Collapsible>
    );
  }

  return (
    <div className="max-h-[40vh] space-y-2 overflow-y-auto sm:max-h-none sm:overflow-visible">
      <div className="flex items-center justify-between gap-2">
        <div className="flex flex-col">
          <span
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className={cn("text-sm font-medium", isActive && "thinking-headline-shimmer")}
          >
            {headline}
          </span>
          {stallLine && <span className="text-xs text-muted-foreground">{stallLine}</span>}
        </div>
        <div className="flex items-center gap-2">
          {isFixture && (
            <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              Demo data
            </span>
          )}
          {isActive && (
            <span aria-hidden="true" className="tabular-nums text-xs text-muted-foreground">
              {formatElapsed(elapsedSeconds)}
            </span>
          )}
          {isActive && onStop && (
            <Button variant="ghost" size="sm" onClick={onStop} className="h-6 gap-1 px-2 text-xs">
              <Square className="size-3" />
              Stop
            </Button>
          )}
        </div>
      </div>
      <div aria-live="off" className="space-y-1.5">
        <RowList rows={rows} collapsedCount={collapsedCount} isFixture={isFixture} isStreaming={isActive} onExpand={() => setExpanded(true)} />
      </div>
      {status === "error" && <ErrorState message={errorMessage} />}
      {status === "aborted" && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-border/60 px-3 py-2 text-xs text-muted-foreground">
          Stopped
          {onRetry && (
            <Button variant="outline" size="sm" onClick={onRetry} className="h-6 px-2 text-xs">
              Retry
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function RowList({
  rows,
  collapsedCount,
  isFixture,
  isStreaming,
  onExpand,
}: {
  rows: ThinkingEvent[];
  collapsedCount: number;
  isFixture: boolean;
  isStreaming: boolean;
  onExpand: () => void;
}) {
  return (
    <>
      {collapsedCount > 0 && (
        <button
          type="button"
          onClick={onExpand}
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          +{collapsedCount} earlier steps
        </button>
      )}
      {rows.map((row) => (
        <ThinkingRow key={row.id} row={row} isStreaming={isStreaming} isFixture={isFixture} />
      ))}
    </>
  );
}
