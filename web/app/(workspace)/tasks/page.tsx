"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, CirclePlay, ListChecks, RefreshCw, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { controlApi, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { cn } from "@/lib/utils";

type TaskKind = "all" | "system" | "sync" | "research" | "exports";
type TaskRow = JsonRecord & { _kind: Exclude<TaskKind, "all">; _kb?: string };

export default function TasksPage() {
  const queryClient = useQueryClient();
  const knowledgeBases = useKnowledgeBases();
  const [kind, setKind] = useState<TaskKind>("all");
  const ha = useQuery({ queryKey: ["tasks", "ha"], queryFn: controlApi.haJobs, retry: false, refetchInterval: 10_000 });
  const exportsQuery = useQuery({ queryKey: ["tasks", "exports"], queryFn: () => controlApi.auditExports(), retry: false, refetchInterval: 10_000 });
  const syncQueries = useQueries({ queries: (knowledgeBases.data ?? []).map((kb) => ({ queryKey: ["tasks", "sync", kb.kb_id], queryFn: () => controlApi.syncJobs(kb.kb_id), retry: false, refetchInterval: 10_000 })) });
  const researchQueries = useQueries({ queries: (knowledgeBases.data ?? []).map((kb) => ({ queryKey: ["tasks", "research", kb.kb_id], queryFn: () => controlApi.researchSummaries(kb.kb_id), retry: false, refetchInterval: 10_000 })) });
  const allRows = useMemo<TaskRow[]>(() => {
    const system: TaskRow[] = records(ha.data, ["jobs", "items"]).map((row) => ({ ...row, _kind: "system" }));
    const exports: TaskRow[] = records(exportsQuery.data, ["jobs", "items", "exports"]).map((row) => ({ ...row, _kind: "exports" }));
    const sync: TaskRow[] = syncQueries.flatMap((taskQuery, index) => records(taskQuery.data, ["jobs", "items"]).map((row) => ({ ...row, _kind: "sync" as const, _kb: knowledgeBases.data?.[index]?.kb_id })));
    const research: TaskRow[] = researchQueries.flatMap((taskQuery, index) => records(taskQuery.data, ["jobs", "items", "summaries"]).map((row) => ({ ...row, _kind: "research" as const, _kb: knowledgeBases.data?.[index]?.kb_id })));
    return [...system, ...sync, ...research, ...exports].sort((a, b) => textValue(b.updated_at, textValue(b.created_at, "")).localeCompare(textValue(a.updated_at, textValue(a.created_at, ""))));
  }, [exportsQuery.data, ha.data, knowledgeBases.data, researchQueries, syncQueries]);
  const visible = kind === "all" ? allRows : allRows.filter((row) => row._kind === kind);
  const isFetching = ha.isFetching || exportsQuery.isFetching || syncQueries.some((item) => item.isFetching) || researchQueries.some((item) => item.isFetching);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["tasks"] });
  const action = useMutation({
    mutationFn: async (row: TaskRow) => {
      const status = textValue(row.status, "").toLowerCase();
      const id = textValue(row.job_id, "");
      if (row._kind === "sync" && row._kb && status === "dead_letter") return controlApi.replaySyncJob(row._kb, id);
      if (row._kind === "research") return controlApi.researchAction(id, status === "paused" ? "resume" : "refresh");
      if (row._kind === "system" && status === "dead_letter") return controlApi.replayHaJob(id, crypto.randomUUID());
      if (row._kind === "system") return controlApi.cancelHaJob(id);
      throw new Error("当前任务没有可用操作");
    },
    onSuccess: async () => { toast.success("任务操作已提交"); await queryClient.invalidateQueries({ queryKey: ["tasks"] }); },
    onError: (error) => toast.error(error.message),
  });
  const label = (row: TaskRow) => textValue(row.title, textValue(row.queue, textValue(row.connection_name, row._kind === "exports" ? "审计导出" : row._kind === "sync" ? "来源同步" : row._kind === "research" ? "Research" : "后台作业")));
  const id = (row: TaskRow) => textValue(row.job_id, textValue(row.id, textValue(row.export_id, "")));
  const href = (row: TaskRow) => row._kind === "research" ? `/research${row._kb ? `?kb=${encodeURIComponent(row._kb)}` : ""}` : row._kind === "sync" && row._kb ? `/knowledge/${encodeURIComponent(row._kb)}/sources` : row._kind === "exports" ? "/admin/audit" : "";
  return (
    <div className="min-h-full">
      <PageHeader eyebrow="Operations" title="任务" description="统一查看入库、同步、Research、审计导出和 HA 作业的真实持久状态。" actions={<Button variant="secondary" onClick={refresh}><RefreshCw className={cn("size-4", isFetching && "animate-spin")} />刷新</Button>} />
      <div className="border-b border-border bg-surface px-5"><Tabs value={kind} onValueChange={(value) => setKind(value as TaskKind)}><TabsList><TabsTrigger value="all">全部 <Badge className="ml-1">{allRows.length}</Badge></TabsTrigger><TabsTrigger value="system">系统</TabsTrigger><TabsTrigger value="sync">同步</TabsTrigger><TabsTrigger value="research">研究</TabsTrigger><TabsTrigger value="exports">导出</TabsTrigger></TabsList></Tabs></div>
      <div className="mx-auto max-w-[1240px] p-4 md:p-6">
        <div className="overflow-hidden border border-border bg-surface">
          <QueryState pending={knowledgeBases.isPending || (ha.isPending && exportsQuery.isPending)} error={knowledgeBases.error} onRetry={refresh} label="正在读取任务" />
          {!knowledgeBases.isPending && !visible.length ? <EmptyState icon={ListChecks} compact title="没有匹配的任务" description="新任务开始后会在这里显示服务端状态、时间和所属资源。" /> : null}
          {visible.length ? <div className="overflow-x-auto"><table className="w-full min-w-[840px] text-left text-[13px]"><thead className="border-b border-border bg-surface-subtle text-[11px] text-muted-foreground"><tr><th className="px-3 py-2 font-medium">任务</th><th className="px-3 py-2 font-medium">类型</th><th className="px-3 py-2 font-medium">知识库</th><th className="px-3 py-2 font-medium">状态</th><th className="px-3 py-2 font-medium">更新时间</th><th className="w-24 px-3 py-2 text-right font-medium">操作</th></tr></thead><tbody className="divide-y divide-border">{visible.map((row, index) => { const taskId = id(row) || String(index); const status = textValue(row.status, "unknown"); const taskHref = href(row); const canAction = (row._kind === "system" && ["queued", "running", "dead_letter"].includes(status)) || (row._kind === "sync" && status === "dead_letter") || row._kind === "research"; return <tr key={`${row._kind}-${taskId}`} className="hover:bg-surface-subtle"><td className="px-3 py-2.5"><p className="font-medium">{label(row)}</p><p className="mt-0.5 max-w-sm truncate font-mono text-[10px] text-muted-foreground">{taskId}</p></td><td className="px-3 py-2.5 text-muted-foreground">{row._kind}</td><td className="px-3 py-2.5 text-muted-foreground">{row._kb || textValue(row.kb_id)}</td><td className="px-3 py-2.5"><StatusBadge status={status} /></td><td className="px-3 py-2.5 font-mono text-[11px] text-muted-foreground">{textValue(row.updated_at, textValue(row.created_at))}</td><td className="px-3 py-2.5"><div className="flex justify-end gap-1">{canAction ? <Button variant="ghost" size="icon" onClick={() => action.mutate(row)} aria-label="执行任务操作">{status === "dead_letter" ? <RotateCcw className="size-3.5" /> : <CirclePlay className="size-3.5" />}</Button> : null}{taskHref ? <Button asChild variant="ghost" size="icon"><Link href={taskHref} aria-label="打开所属资源"><ArrowUpRight className="size-3.5" /></Link></Button> : null}</div></td></tr>; })}</tbody></table></div> : null}
        </div>
      </div>
    </div>
  );
}
