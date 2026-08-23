import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function PageHeader({
  eyebrow,
  title,
  description,
  meta,
  actions,
  className,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-h-[84px] items-start justify-between gap-6 border-b border-border bg-surface px-5 py-4 md:px-7", className)}>
      <div className="min-w-0">
        {eyebrow ? <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-primary">{eyebrow}</p> : null}
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="truncate text-xl font-semibold tracking-[-0.018em]">{title}</h2>
          {meta}
        </div>
        {description ? <p className="mt-1 max-w-3xl text-[13px] leading-5 text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}
