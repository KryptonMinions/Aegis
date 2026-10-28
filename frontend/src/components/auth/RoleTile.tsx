import type { LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function RoleTile({
  label,
  description,
  icon: Icon,
  onClick,
  disabled = false,
}: {
  label: string;
  description: string;
  icon: LucideIcon;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Card
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onClick={() => !disabled && onClick()}
      onKeyDown={(e) => !disabled && (e.key === "Enter" || e.key === " ") && onClick()}
      className={cn(
        "cursor-pointer items-start gap-3 p-5 transition-colors hover:bg-muted/60",
        disabled && "pointer-events-none opacity-60",
        "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
      )}
    >
      <Icon className="size-7 text-primary" />
      <div>
        <div className="font-medium">{label}</div>
        <div className="text-sm text-muted-foreground">{description}</div>
      </div>
    </Card>
  );
}
