import { cn } from "@/lib/utils";

const sizes = {
  sm: "size-3.5 border-[1.5px]",
  md: "size-4 border-2",
  lg: "size-6 border-2",
} as const;

export function Spinner({
  size = "md",
  className,
}: {
  size?: keyof typeof sizes;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-block shrink-0 animate-spin rounded-full border-current border-r-transparent motion-reduce:animate-none",
        sizes[size],
        className,
      )}
    />
  );
}

export function LoadingState({
  label = "正在加载",
  page = false,
  className,
}: {
  label?: string;
  page?: boolean;
  className?: string;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex items-center justify-center gap-3 text-[13px] text-muted-foreground",
        page ? "min-h-dvh bg-surface" : "min-h-40",
        className,
      )}
    >
      <Spinner size={page ? "lg" : "md"} className="text-primary" />
      <span>{label}</span>
    </div>
  );
}
