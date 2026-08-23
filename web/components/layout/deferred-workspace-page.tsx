import type { LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";

export function DeferredWorkspacePage({ icon: Icon, title, description, capabilities }: { icon: LucideIcon; title: string; description: string; capabilities: string[] }) {
  return (
    <div className="mx-auto w-full max-w-[960px] p-4 md:p-6">
      <div className="mb-6"><div className="mb-3 flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface text-primary"><Icon className="size-[18px]" /></div><div className="flex items-center gap-2"><h2 className="text-2xl font-semibold tracking-[-0.02em]">{title}</h2><Badge>迁移中</Badge></div><p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</p></div>
      <div className="overflow-hidden rounded-[5px] border border-border bg-surface"><div className="border-b border-border bg-surface-subtle px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">现有能力</div><ul className="divide-y divide-border">{capabilities.map((capability) => <li key={capability} className="px-3 py-3 text-[13px]">{capability}</li>)}</ul></div>
      <p className="mt-3 text-xs text-muted-foreground">这些后端能力保持不变，当前阶段继续由原有工作台提供操作入口。</p>
    </div>
  );
}
