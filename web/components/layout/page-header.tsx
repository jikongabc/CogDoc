import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function PageHeader({
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
    <div className={cn("flex min-h-[88px] flex-col gap-4 border-b border-border bg-surface px-4 py-5 sm:px-6 xl:flex-row xl:items-start xl:justify-between xl:px-8", className)}>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="text-xl font-semibold tracking-[-0.025em]">{title}</h2>
          {meta}
        </div>
        {description ? <p className="mt-1 max-w-3xl text-[13px] leading-5 text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex w-full flex-wrap items-center gap-2 xl:w-auto xl:shrink-0 xl:justify-end">{actions}</div> : null}
    </div>
  );
}
