import { AlertTriangle, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/spinner";

export function QueryState({
  pending,
  error,
  onRetry,
  label = "正在读取数据",
  errorTitle = "无法读取数据",
}: {
  pending?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  label?: string;
  errorTitle?: string;
}) {
  if (pending) {
    return <LoadingState label={label} />;
  }
  if (error) {
    return (
      <div role="alert" className="flex min-h-40 items-center justify-center px-5 text-center">
        <div className="max-w-sm">
          <AlertTriangle className="mx-auto size-5 text-error" />
          <p className="mt-2 text-sm font-medium">{errorTitle}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{error.message}</p>
          {onRetry ? <Button variant="secondary" size="compact" className="mt-3" onClick={onRetry}><RotateCw className="size-3.5" />重试</Button> : null}
        </div>
      </div>
    );
  }
  return null;
}
