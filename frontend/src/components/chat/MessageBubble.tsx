import type { ChatMessage } from "@/lib/types/content-blocks";
import { cn } from "@/lib/utils";
import { BlockRenderer } from "./BlockRenderer";
import { ThinkingPanel } from "./ThinkingPanel";

interface MessageBubbleProps {
  message: ChatMessage;
  onStop?: () => void;
  onRetry?: () => void;
}

export function MessageBubble({ message, onStop, onRetry }: MessageBubbleProps) {
  const isUser = message.role === "user";
  // thinking_state.md T-4/AC-1: paint the panel before the first thinking
  // row arrives, not just once rows exist.
  const showThinking =
    !isUser && ((message.thinking && message.thinking.length > 0) || (message.isStreaming && message.status !== "done"));

  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[92%] space-y-2 sm:max-w-[85%]",
          isUser
            ? "rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground"
            : "w-full",
        )}
      >
        {isUser && message.text && <p>{message.text}</p>}
        {!isUser && (
          <div className="space-y-3">
            {showThinking && (
              <ThinkingPanel
                thinking={message.thinking}
                isStreaming={message.isStreaming}
                status={message.status}
                startedAt={message.startedAt}
                endedAt={message.endedAt}
                currentPhase={message.currentPhase}
                currentAgent={message.currentAgent}
                onStop={onStop}
                onRetry={onRetry}
                errorMessage={message.errorMessage}
              />
            )}
            {message.text && <p className="text-sm">{message.text}</p>}
            {message.blocks?.map((block) => (
              <BlockRenderer key={block.id} block={block} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
