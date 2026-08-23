import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  compact = false,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center justify-center text-center", compact ? "min-h-40 px-5 py-7" : "min-h-64 px-6 py-10", className)}>
      <div className="max-w-sm">
        <span className="mx-auto flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface-subtle text-muted-foreground">
          <Icon className="size-[17px]" />
        </span>
        <h3 className="mt-3 text-sm font-semibold">{title}</h3>
        <p className="mt-1 text-[13px] leading-5 text-muted-foreground">{description}</p>
        {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
      </div>
    </div>
  );
}
