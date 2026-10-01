import type { ChatMessage, ThinkingEvent } from "@/lib/types/content-blocks";
import type { AskInput } from "./ask-service";
import { AskServiceError, InvalidRequestError, UnauthenticatedError } from "./ask-service";
import {
  buildAskRequestBody,
  wireResponseToChatMessage,
  type WireAskResponse,
} from "./real-ask-service";
import { getAccessToken } from "@/lib/auth/auth-context";

export type StreamEvent =
  | { type: "thinking"; event: ThinkingEvent }
  | { type: "message"; message: ChatMessage };

/**
 * SSE client for POST /ask/stream (ASK_STREAM_ENDPOINT_CONTRACT.md §2/§3).
 * Uses fetch + a manual ReadableStream reader rather than EventSource,
 * because EventSource can't send the Authorization header this endpoint
 * requires. Yields `thinking` events as they arrive, then resolves with the
 * final `message` once the terminal SSE event lands; throws a typed error
 * (matching RealAskService's error mapping) on a pre-flight failure or a
 * mid-stream `error` SSE event.
 */
export interface StreamAskOptions {
  /** Aborts the underlying fetch (thinking_state.md §11 Stop control). */
  signal?: AbortSignal;
  /** Fires once the response headers land (HTTP 200 committed), before any
   * `thinking` frame has arrived -- the `submitting` -> `waiting` boundary. */
  onOpen?: () => void;
}

export async function* streamAsk(
  input: AskInput,
  options: StreamAskOptions = {},
): AsyncGenerator<StreamEvent, void, void> {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL;
  if (!apiUrl) {
    throw new Error("NEXT_PUBLIC_API_URL is required for streaming");
  }
  const accessToken = getAccessToken();
  if (!accessToken) {
    throw new UnauthenticatedError("No active session");
  }

  let res: Response;
  try {
    res = await fetch(`${apiUrl}/ask/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify(buildAskRequestBody(input)),
      signal: options.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") {
      throw new AskServiceError("aborted", "Request was stopped.");
    }
    throw new AskServiceError("network_error", (err as Error).message);
  }

  options.onOpen?.();

  if (!res.ok) {
    // Pre-flight failure (401/422/etc.) -- the stream never opened, this is
    // still a plain JSON error body (ASK_STREAM_ENDPOINT_CONTRACT.md §2.1).
    const body = await res.json().catch(() => null);
    const code: string = body?.error?.code ?? "unknown_error";
    const message: string = body?.error?.message ?? "The request could not be completed.";
    if (res.status === 401) throw new UnauthenticatedError(message);
    if (res.status === 422) throw new InvalidRequestError(message);
    throw new AskServiceError(code, message, res.status);
  }
  if (!res.body) {
    throw new AskServiceError("network_error", "Streaming response had no body");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex: number;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const parsed = parseSseFrame(frame);
        if (!parsed) continue; // keepalive comment or empty frame

        if (parsed.event === "thinking") {
          yield { type: "thinking", event: parsed.data as ThinkingEvent };
        } else if (parsed.event === "message") {
          yield { type: "message", message: wireResponseToChatMessage(parsed.data as WireAskResponse) };
          return;
        } else if (parsed.event === "error") {
          const err = parsed.data as { error?: { code?: string; message?: string } };
          throw new AskServiceError(
            err.error?.code ?? "unknown_error",
            err.error?.message ?? "Something went wrong while streaming the response.",
          );
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseFrame(frame: string): { event: string; data: unknown } | null {
  if (!frame || frame.startsWith(":")) return null; // keepalive comment
  let event: string | null = null;
  let data: string | null = null;
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice("event: ".length);
    else if (line.startsWith("data: ")) data = line.slice("data: ".length);
  }
  if (!event || data === null) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return null;
  }
}
