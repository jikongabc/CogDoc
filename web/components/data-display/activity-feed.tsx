import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface ActivityItem {
  id: string;
  actor: string;
  action: ReactNode;
  timestamp: string;
  initials?: string;
}

export function ActivityFeed({ items, className }: { items: ActivityItem[]; className?: string }) {
  return (
    <ul className={cn("divide-y divide-border", className)}>
      {items.map((item) => (
        <li key={item.id} className="flex gap-3 py-3">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-surface-subtle text-[11px] font-semibold text-muted-foreground">{item.initials ?? item.actor.slice(0, 2).toUpperCase()}</div>
          <div className="min-w-0 flex-1 text-[13px]"><span className="font-medium">{item.actor}</span> {item.action}<div className="mt-0.5 text-[11px] text-muted-foreground">{item.timestamp}</div></div>
        </li>
      ))}
    </ul>
  );
}
