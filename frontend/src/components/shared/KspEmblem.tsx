import Image from "next/image";
import { cn } from "@/lib/utils";

/** KSP / Karnataka Police emblem + wordmark — anchors the login screen and app headers. */
export function KspEmblem({
  compact,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <Image
        src="/Seal_of_Karnataka.svg"
        alt="Seal of Karnataka"
        width={24}
        height={24}
        className="size-6 shrink-0"
      />
      {!compact && (
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-wide">Karnataka State Police</div>
          <div className="text-[11px] text-muted-foreground">Ask Platform</div>
        </div>
      )}
    </div>
  );
}
