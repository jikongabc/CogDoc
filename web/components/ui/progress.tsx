import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";

export function Progress({
  value,
  label = "正在处理",
  className,
}: {
  value?: number;
  label?: string;
  className?: string;
}) {
  const determinate = typeof value === "number";
  const normalizedValue = determinate ? Math.max(0, Math.min(100, value)) : undefined;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? 100 : undefined}
      aria-valuenow={normalizedValue}
      aria-valuetext={determinate ? `${normalizedValue}%` : label}
      className={cn("relative h-1.5 overflow-hidden rounded-full bg-surface-subtle", className)}
    >
      <span
        className={cn(
          "absolute inset-y-0 left-0 rounded-full bg-primary",
          determinate ? "transition-[width] duration-300" : "cogdoc-progress-indeterminate w-2/5",
        )}
        style={determinate ? ({ width: `${normalizedValue}%` } as CSSProperties) : undefined}
      />
    </div>
  );
}
