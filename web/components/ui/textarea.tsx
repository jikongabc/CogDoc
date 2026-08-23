import * as React from "react";
import { cn } from "@/lib/utils";

export function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      className={cn(
        "min-h-20 w-full resize-y rounded-[5px] border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground/75 focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:bg-surface-subtle",
        className,
      )}
      {...props}
    />
  );
}
