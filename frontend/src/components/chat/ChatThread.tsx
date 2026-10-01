"use client";

import { useEffect, useRef, useState } from "react";
import { Download, Loader2 } from "lucide-react";
import type { ChatMessage, ThinkingEvent } from "@/lib/types/content-blocks";
import { useAuth } from "@/lib/auth/auth-context";
import { useAsk } from "@/lib/ask/use-ask";
import { useAskStream } from "@/lib/ask/use-ask-stream";
import { askService } from "@/lib/ask/index";
import { RealAskService } from "@/lib/ask/real-ask-service";
import { exportSessionPdf } from "@/lib/export/export-pdf";
import { Button } from "@/components/ui/button";
import { MessageBubble } from "./MessageBubble";
import { QueryInput, type SendMeta } from "./QueryInput";
import { ErrorState } from "@/components/shared/ErrorState";
import { ThinkingPanel } from "./ThinkingPanel";

// Resolved once at module load, mirroring the mock/real toggle in
// lib/ask/index.ts -- MockAskService (offline dev, NEXT_PUBLIC_ASK_SERVICE=
// mock) never supports streaming, so this stays a binary check rather than
// a separate capability-flag abstraction (ASK_STREAM_ENDPOINT_CONTRACT.md §3).
// thinking_state.md §10: NEXT_PUBLIC_ASK_TRANSPORT=sync forces the
// non-streaming path regardless of service capability.
const supportsStreaming =
  askService instanceof RealAskService && process.env.NEXT_PUBLIC_ASK_TRANSPORT !== "sync";
// thinking_state.md §10: v1 (default) keeps today's exact behavior below,
// including filtering a failed placeholder out of the thread.
const THINKING_UI_V2 = process.env.NEXT_PUBLIC_THINKING_UI === "v2";

interface ChatThreadProps {
  /** Larger touch targets for the IO MobileShell (steering-docs §9). */
  inputSize?: "default" | "large";
  emptyState?: React.ReactNode;
}

/** The core conversational surface, shared identically by every role (steering-docs §1). */
export function ChatThread({ inputSize = "default", emptyState }: ChatThreadProps) {
  const { user } = useAuth();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Null starts a new server-side thread; set once the first response
  // echoes one back (ASK_ENDPOINT_CONTRACT.md §2.3/§2.4). turnIndex is
  // 0-based and monotonic per thread, tracked here since the service layer
  // is stateless between calls.
  const [threadId, setThreadId] = useState<string | null>(null);
  const [turnIndex, setTurnIndex] = useState(0);
  const { mutateAsync, isPending: isMutationPending, isError: isMutationError, error: mutationError } = useAsk();
  const { send: sendStream, stop: stopStream, isStreaming } = useAskStream();
  // True from send-start until the first thinking/message/error event lands
  // -- keeps the generic skeleton up for just the pre-first-byte gap, then
  // the placeholder assistant message's own ThinkingPanel takes over.
  const [isAwaitingFirstEvent, setIsAwaitingFirstEvent] = useState(false);
  const [streamError, setStreamError] = useState<Error | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  // thinking_state.md §10 sync-transport degradation: headline-only timer
  // state for the non-streaming path, when v2 is on.
  const [syncStartedAt, setSyncStartedAt] = useState<number | undefined>(undefined);
  // thinking_state.md §11: last submission, so the Retry action on an
  // error/aborted panel can resend without the user retyping.
  const lastSendRef = useRef<{ query: string; meta: SendMeta } | null>(null);

  const isPending = supportsStreaming ? isStreaming : isMutationPending;
  const isError = supportsStreaming ? streamError !== null : isMutationError;
  const error = supportsStreaming ? streamError : mutationError;
  const showLoadingSkeleton = THINKING_UI_V2
    ? !supportsStreaming && isPending
    : supportsStreaming
      ? isPending && isAwaitingFirstEvent
      : isPending;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isPending]);

  const handleExport = async () => {
    setExportError(null);
    setIsExporting(true);
    try {
      await exportSessionPdf(messages, threadId);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setIsExporting(false);
    }
  };

  const handleSendStreaming = async (query: string, meta: SendMeta) => {
    lastSendRef.current = { query, meta };
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      timestamp: new Date().toISOString(),
      text: query,
    };
    const placeholderId = `assistant-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      userMessage,
      {
        id: placeholderId,
        role: "assistant",
        timestamp: new Date().toISOString(),
        isStreaming: true,
        thinking: [],
        // thinking_state.md T-4: startedAt is set on submitting, before the
        // fetch even resolves headers.
        status: "submitting",
        startedAt: Date.now(),
      },
    ]);
    setStreamError(null);
    setIsAwaitingFirstEvent(true);

    await sendStream(
      {
        query,
        threadId,
        turnIndex,
        role: user?.role ?? "investigating_officer",
        detectedLanguage: meta.detectedLanguage,
        inputModality: meta.inputModality,
      },
      {
        onOpen: () => {
          setMessages((prev) =>
            prev.map((m) => (m.id === placeholderId ? { ...m, status: "waiting" } : m)),
          );
        },
        onThinking: (event: ThinkingEvent) => {
          setIsAwaitingFirstEvent(false);
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== placeholderId) return m;
              const next: ChatMessage = {
                ...m,
                thinking: [...(m.thinking ?? []), event],
                status: m.status === "submitting" || m.status === "waiting" ? "streaming" : m.status,
              };
              if (event.kind === "phase" && event.phase) {
                next.currentPhase = event.phase;
                if (event.phase === "composing") next.status = "composing";
              }
              if (event.kind === "agent_selected" && event.agent) {
                next.currentAgent = event.agent;
              }
              return next;
            }),
          );
        },
        onDone: (finalMessage: ChatMessage) => {
          setIsAwaitingFirstEvent(false);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === placeholderId
                ? {
                    ...finalMessage,
                    id: placeholderId,
                    thinking: m.thinking,
                    isStreaming: false,
                    status: "done",
                    startedAt: m.startedAt,
                    endedAt: Date.now(),
                  }
                : m,
            ),
          );
          if (finalMessage.threadId) setThreadId(finalMessage.threadId);
          setTurnIndex((i) => i + 1);
        },
        onError: (err: Error) => {
          setIsAwaitingFirstEvent(false);
          setStreamError(err);
          if (THINKING_UI_V2) {
            // Freeze the placeholder in place with its rows and an inline
            // ErrorState + retry, rather than leaving a ghost bubble.
            setMessages((prev) =>
              prev.map((m) =>
                m.id === placeholderId
                  ? { ...m, isStreaming: false, status: "error", endedAt: Date.now(), errorMessage: err.message }
                  : m,
              ),
            );
          } else {
            // v1 behavior, unchanged (thinking_state.md AC-6): a transport
            // failure doesn't leave a ghost bubble in the thread, the
            // thread-level ErrorState below renders it instead.
            setMessages((prev) => prev.filter((m) => m.id !== placeholderId));
          }
        },
        onAbort: () => {
          setIsAwaitingFirstEvent(false);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === placeholderId ? { ...m, isStreaming: false, status: "aborted", endedAt: Date.now() } : m,
            ),
          );
        },
      },
    );
  };

  const handleStop = () => {
    if (THINKING_UI_V2) stopStream();
  };

  const handleRetry = () => {
    if (!lastSendRef.current) return;
    void handleSendStreaming(lastSendRef.current.query, lastSendRef.current.meta);
  };

  useEffect(() => {
    if (!THINKING_UI_V2) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isStreaming) stopStream();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isStreaming, stopStream]);

  const handleSendNonStreaming = async (query: string, meta: SendMeta) => {
    lastSendRef.current = { query, meta };
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      timestamp: new Date().toISOString(),
      text: query,
    };
    setMessages((prev) => [...prev, userMessage]);
    setSyncStartedAt(Date.now());

    try {
      const response = await mutateAsync({
        query,
        threadId,
        turnIndex,
        role: user?.role ?? "investigating_officer",
        detectedLanguage: meta.detectedLanguage,
        inputModality: meta.inputModality,
      });
      setMessages((prev) => [...prev, response]);
      if (response.threadId) setThreadId(response.threadId);
      setTurnIndex((i) => i + 1);
    } catch {
      // A transport/validation failure is a distinct, retryable state from
      // `no_answer` (a normal 200 response) -- isError/error below render
      // it; nothing to append to the thread here (§3.5).
    }
  };

  const handleSend = supportsStreaming ? handleSendStreaming : handleSendNonStreaming;

  return (
    <div className="flex h-full flex-col">
      {messages.length > 0 && (
        <div className="flex items-center justify-end gap-2 border-b border-border px-4 py-1.5">
          {exportError && <span className="text-xs text-destructive">{exportError}</span>}
          <Button variant="outline" size="sm" onClick={handleExport} disabled={isExporting}>
            {isExporting ? <Loader2 className="animate-spin" /> : <Download />}
            Export PDF
          </Button>
        </div>
      )}
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length === 0 && emptyState}
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} onStop={handleStop} onRetry={handleRetry} />
        ))}
        {showLoadingSkeleton && THINKING_UI_V2 ? (
          <ThinkingPanel status={isPending ? "waiting" : "done"} startedAt={syncStartedAt} />
        ) : (
          showLoadingSkeleton && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 shrink-0 animate-spin" />
              Thinking…
            </div>
          )
        )}
        {isError && <ErrorState message={error instanceof Error ? error.message : undefined} />}
        <div ref={bottomRef} />
      </div>
      <QueryInput onSend={handleSend} disabled={isPending} size={inputSize} />
    </div>
  );
}
