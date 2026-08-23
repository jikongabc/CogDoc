import type { ReactNode } from "react";
import { Check, Circle, LoaderCircle, X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface TimelineItem {
  id: string;
  title: string;
  detail?: ReactNode;
  time?: string;
  state?: "complete" | "current" | "failed" | "pending";
}

export function Timeline({ items, className }: { items: TimelineItem[]; className?: string }) {
  const icons = {
    complete: Check,
    current: LoaderCircle,
    failed: X,
    pending: Circle,
  };
  return (
    <ol className={cn("space-y-0", className)}>
      {items.map((item, index) => {
        const state = item.state ?? "pending";
        const Icon = icons[state];
        return (
          <li key={item.id} className="relative flex gap-3 pb-5 last:pb-0">
            {index < items.length - 1 ? <span className="absolute left-[7px] top-4 h-[calc(100%-8px)] w-px bg-border" /> : null}
            <Icon className={cn("relative z-10 mt-0.5 size-4 bg-surface text-muted-foreground", state === "complete" && "text-success", state === "current" && "animate-spin text-primary", state === "failed" && "text-error")} />
            <div className="min-w-0 flex-1"><div className="flex justify-between gap-3"><span className="text-[13px] font-medium">{item.title}</span>{item.time ? <span className="text-[11px] text-muted-foreground">{item.time}</span> : null}</div>{item.detail ? <div className="mt-1 text-xs text-muted-foreground">{item.detail}</div> : null}</div>
          </li>
        );
      })}
    </ol>
  );
}
