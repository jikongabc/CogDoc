import * as React from "react";
import { cn } from "@/lib/utils";

export function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "h-9 w-full rounded-[5px] border border-border bg-surface px-3 text-sm text-foreground shadow-[var(--shadow-edge)] outline-none placeholder:text-muted-foreground/75 focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:bg-surface-subtle disabled:opacity-70",
        className,
      )}
      {...props}
    />
  );
}
