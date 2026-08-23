import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface PermissionOption {
  id: string;
  label: string;
  description: string;
  enabled: boolean;
  inherited?: boolean;
}

export function PermissionEditor({ options, onChange, className }: { options: PermissionOption[]; onChange?: (id: string, enabled: boolean) => void; className?: string }) {
  return (
    <div className={cn("divide-y divide-border rounded-[5px] border border-border bg-surface", className)}>
      {options.map((option) => (
        <label key={option.id} className="flex min-h-14 items-center gap-3 px-3 py-2">
          <input type="checkbox" checked={option.enabled} disabled={option.inherited} onChange={(event) => onChange?.(option.id, event.target.checked)} className="size-4 accent-primary" />
          <span className="min-w-0 flex-1"><span className="font-medium">{option.label}</span><span className="mt-0.5 block text-xs text-muted-foreground">{option.description}</span></span>
          {option.inherited ? <Badge>继承</Badge> : null}
        </label>
      ))}
    </div>
  );
}
