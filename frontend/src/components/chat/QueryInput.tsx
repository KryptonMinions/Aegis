"use client";

import { useState, type ChangeEvent, type KeyboardEvent } from "react";
import Image from "next/image";
import { Loader2, SendHorizonal } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth/auth-context";
import { useVoiceRecorder } from "@/lib/voice/use-voice-recorder";

export interface SendMeta {
  detectedLanguage?: "en" | "kn" | null;
  inputModality: "voice" | "typed";
}

interface QueryInputProps {
  onSend: (query: string, meta: SendMeta) => void;
  disabled?: boolean;
  /** Larger, touch-first sizing for the IO MobileShell (steering-docs §9). */
  size?: "default" | "large";
  placeholder?: string;
}

// Runtime kill-switch (steering-docs/VOICE_INTAKE_STEERING.md §7) — mic button
// is hidden (not greyed) when off; text input is unaffected either way.
const VOICE_INTAKE_ENABLED = process.env.NEXT_PUBLIC_VOICE_INTAKE_ENABLED !== "false";

/** Shared, shell-agnostic query input — sized via a prop, not a second component (steering-docs §1). */
export function QueryInput({
  onSend,
  disabled,
  size = "default",
  placeholder = "Ask me Anything...",
}: QueryInputProps) {
  const { user } = useAuth();
  const [value, setValue] = useState("");
  const [voiceMeta, setVoiceMeta] = useState<{ detectedLanguage: "en" | "kn" | null } | null>(
    null
  );

  const { status: voiceStatus, error: voiceError, toggle: toggleVoice } = useVoiceRecorder({
    accessToken: user?.accessToken,
    onTranscript: (transcript, detectedLanguage) => {
      setValue(transcript);
      setVoiceMeta({ detectedLanguage });
    },
  });

  const isProcessingVoice = voiceStatus === "processing";
  const isRecording = voiceStatus === "recording";
  const inputDisabled = disabled || isProcessingVoice;

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const next = event.target.value;
    setValue(next);
    if (next === "") setVoiceMeta(null);
  };

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(
      trimmed,
      voiceMeta
        ? { detectedLanguage: voiceMeta.detectedLanguage, inputModality: "voice" }
        : { inputModality: "typed" }
    );
    setValue("");
    setVoiceMeta(null);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-border bg-background p-3">
      <div
        className={cn(
          "flex flex-col rounded-3xl border border-white/25 bg-white/10 shadow-lg backdrop-blur-xl",
          "dark:border-white/10 dark:bg-white/5",
        )}
      >
        <textarea
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={isProcessingVoice ? "Transcribing..." : placeholder}
          rows={size === "large" ? 2 : 1}
          disabled={inputDisabled}
          className={cn(
            "resize-none bg-transparent px-4 pt-3 text-sm outline-none placeholder:text-muted-foreground",
            size === "large" && "text-base",
          )}
        />
        {voiceError && (
          <p className="px-4 pt-1 text-xs text-destructive" role="alert">
            {voiceError}
          </p>
        )}
        <div className="flex items-center justify-end gap-2 px-2.5 pb-2.5 pt-1">
          {VOICE_INTAKE_ENABLED && (
            <button
              type="button"
              onClick={toggleVoice}
              disabled={disabled || isProcessingVoice}
              title="Speak your question"
              aria-pressed={isRecording}
              className={cn(
                "inline-flex shrink-0 items-center justify-center rounded-full border shadow-sm backdrop-blur-md transition-colors",
                "disabled:opacity-40",
                isRecording
                  ? "border-accent/50 bg-accent/40 ring-2 ring-accent/40 animate-pulse"
                  : "border-black/10 bg-black/6 hover:bg-black/10 dark:border-white/15 dark:bg-white/10 dark:hover:bg-white/20",
                size === "large" ? "size-11" : "size-8",
              )}
              aria-label={isRecording ? "Stop voice input" : "Start voice input"}
            >
              {isProcessingVoice ? (
                <Loader2
                  className={cn("animate-spin", size === "large" ? "size-5" : "size-4")}
                />
              ) : (
                <Image
                  src="/voice-svgrepo-com.svg"
                  alt=""
                  width={20}
                  height={20}
                  className={size === "large" ? "size-5" : "size-4"}
                />
              )}
            </button>
          )}
          <button
            type="button"
            onClick={submit}
            disabled={inputDisabled || !value.trim()}
            className={cn(
              "inline-flex shrink-0 items-center justify-center rounded-full border border-white/20 bg-primary/85 text-primary-foreground backdrop-blur-md transition-opacity",
              "disabled:opacity-40",
              size === "large" ? "size-11" : "size-8",
            )}
            aria-label="Send"
          >
            <SendHorizonal className={size === "large" ? "size-5" : "size-4"} />
          </button>
        </div>
      </div>
    </div>
  );
}
