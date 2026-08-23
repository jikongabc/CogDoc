"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Activity, Bug, CheckCircle2, Gauge, Play, RotateCcw, ScanSearch } from "lucide-react";
import { useParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api/client";
import { controlApi, isRecord, records, textValue } from "@/lib/api/control-plane";
import { decodeRouteParam } from "@/lib/routing";
import { cn } from "@/lib/utils";

interface RetrievalForm {
  query: string;
  topK: number;
  rerank: boolean;
}

export default function DiagnosticsPage() {
  const params = useParams<{ kbId: string }>();
  const kbId = decodeRouteParam(params.kbId);
  const [traceId, setTraceId] = useState("");
  const [migrationRunId, setMigrationRunId] = useState("");
  const traces = useQuery({ queryKey: ["diagnostics", "traces", kbId], queryFn: () => controlApi.traces(kbId) });
  const traceRows = records(traces.data, ["traces", "items"]);
  const activeTraceId = traceId || textValue(traceRows[0]?.trace_id, "");
  const trace = useQuery({ queryKey: ["diagnostics", "trace", activeTraceId], queryFn: () => api.trace(activeTraceId), enabled: Boolean(activeTraceId) });
  const retrievalForm = useForm<RetrievalForm>({ defaultValues: { query: "", topK: 12, rerank: true } });
  const diagnose = useMutation({ mutationFn: (values: RetrievalForm) => controlApi.diagnoseRetrieval(kbId, values.query, Number(values.topK), values.rerank) });
  const migrations = useQuery({ queryKey: ["diagnostics", "migrations"], queryFn: controlApi.scanIndexMigrations, retry: false });
  const migrationRows = records(migrations.data, ["items", "knowledge_bases", "entries", "scan"]);
  const run = useQuery({
    queryKey: ["diagnostics", "migration-run", migrationRunId],
    queryFn: () => controlApi.indexMigration(migrationRunId),
    enabled: Boolean(migrationRunId),
    refetchInterval: (query) => {
      const value = isRecord(query.state.data) ? query.state.data : {};
      return ["queued", "running", "pending"].includes(textValue(value.status, "").toLowerCase()) ? 3000 : false;
    },
  });
  const startMigration = useMutation({
    mutationFn: async () => {
      const value = await controlApi.startIndexMigration([kbId]);
      const row = isRecord(value) ? value : {};
      const runId = textValue(row.run_id, "");
      if (!runId) throw new Error("迁移接口未返回 run_id");
      return runId;
    },
    onSuccess: (runId) => {
      setMigrationRunId(runId);
      toast.success("索引迁移已进入后台队列");
    },
    onError: (error) => toast.error(error.message),
  });
  const migrationAction = useMutation({
    mutationFn: (action: "rollback" | "finalize") => action === "rollback" ? controlApi.rollbackIndexMigration(migrationRunId) : controlApi.finalizeIndexMigration(migrationRunId),
    onSuccess: async (_, action) => {
      toast.success(action === "rollback" ? "已回切到迁移前索引代" : "旧索引代已清理");
      await run.refetch();
      await migrations.refetch();
    },
    onError: (error) => toast.error(error.message),
  });

  const diagnosticRow = isRecord(diagnose.data) ? diagnose.data : {};
  const hitRows = records(diagnosticRow, ["hits", "items", "results"]);
  const traceSteps = trace.data?.steps ?? [];
  const runRow = isRecord(run.data) ? run.data : {};
  const runStatus = textValue(runRow.status, "").toLowerCase();

  return (
    <div className="min-h-full">
      <PageHeader eyebrow="Operator tools" title="诊断" description="检查 Trace、检索路径和索引代际。诊断术语只出现在此运维页面。" />
      <div className="p-4 md:p-6">
        <Tabs defaultValue="traces">
          <TabsList className="mb-4"><TabsTrigger value="traces">Trace 调试 <Badge className="ml-1">{traceRows.length}</Badge></TabsTrigger><TabsTrigger value="retrieval">检索诊断</TabsTrigger><TabsTrigger value="migrations">索引代际</TabsTrigger></TabsList>
          <TabsContent value="traces">
            <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
              <section className="overflow-hidden border border-border bg-surface">
                <QueryState pending={traces.isPending} error={traces.error} onRetry={() => void traces.refetch()} />
                {traces.data && !traceRows.length ? <EmptyState icon={Activity} compact title="没有 Trace" description="执行对话或检索后，可在这里检查节点、耗时和错误。" /> : (
                  <div className="divide-y divide-border">{traceRows.map((row) => { const id = textValue(row.trace_id); return <button key={id} onClick={() => setTraceId(id)} className={cn("w-full px-4 py-3 text-left hover:bg-surface-subtle", activeTraceId === id && "bg-primary-subtle")}><div className="flex items-center justify-between gap-2"><p className="truncate text-[13px] font-medium">{textValue(row.query_preview, textValue(row.task_type, "请求"))}</p><StatusBadge status={textValue(row.status, "unknown")} /></div><p className="mt-1 font-mono text-[10px] text-muted-foreground">{id}</p></button>; })}</div>
                )}
              </section>
              <section className="overflow-hidden border border-border bg-surface">
                <QueryState pending={trace.isPending} error={trace.error} onRetry={() => void trace.refetch()} />
                {trace.data ? <><div className="grid gap-px border-b border-border bg-border sm:grid-cols-4">{[["任务", trace.data.task_type], ["状态", trace.data.status], ["耗时", trace.data.duration_ms ? `${Math.round(trace.data.duration_ms)} ms` : "—"], ["证据完整度", trace.data.evidence_completeness ?? "—"]].map(([label, value]) => <div key={label} className="bg-surface px-3 py-2.5"><p className="text-[10px] text-muted-foreground">{label}</p><p className="mt-0.5 truncate text-xs font-medium">{String(value)}</p></div>)}</div><div className="divide-y divide-border">{traceSteps.map((step, index) => <div key={index} className="px-4 py-3"><div className="flex items-center justify-between gap-3"><p className="text-[13px] font-medium">{textValue(step.node_name as never, textValue(step.name as never, `步骤 ${index + 1}`))}</p><span className="font-mono text-[10px] text-muted-foreground">{textValue(step.duration_ms as never, "")}</span></div><pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded-[3px] bg-surface-subtle p-2 font-mono text-[10px] leading-4 text-muted-foreground">{JSON.stringify(step, null, 2)}</pre></div>)}</div></> : !activeTraceId ? <EmptyState icon={Bug} title="选择 Trace" description="查看请求配置、执行步骤和稳定错误信息。" /> : null}
              </section>
            </div>
          </TabsContent>
          <TabsContent value="retrieval">
            <section className="border border-border bg-surface">
              <form className="flex flex-wrap items-end gap-3 border-b border-border p-4" onSubmit={retrievalForm.handleSubmit((values) => diagnose.mutate(values))}><div className="min-w-[260px] flex-1 space-y-1.5"><Label htmlFor="diagnostic-query">检索问题</Label><Input id="diagnostic-query" placeholder="输入需要检查的真实查询" {...retrievalForm.register("query", { required: true })} /></div><div className="w-24 space-y-1.5"><Label htmlFor="top-k">Top K</Label><Input id="top-k" type="number" {...retrievalForm.register("topK", { valueAsNumber: true })} /></div><label className="flex h-9 items-center gap-2 text-xs"><input type="checkbox" className="size-4 accent-primary" {...retrievalForm.register("rerank")} />启用重排</label><Button type="submit" variant="primary" loading={diagnose.isPending}><Play className="size-4" />运行诊断</Button></form>
              {diagnose.error ? <p className="m-4 border-l-2 border-error bg-error-subtle p-3 text-xs text-error">{diagnose.error.message}</p> : null}
              {hitRows.length ? <div className="divide-y divide-border">{hitRows.map((row, index) => <div key={textValue(row.chunk_id, String(index))} className="grid grid-cols-[40px_minmax(0,1fr)_130px] gap-3 px-4 py-3"><span className="font-mono text-[10px] text-muted-foreground">#{index + 1}</span><div><p className="text-[13px] font-medium">{textValue(row.source, textValue(row.title, "检索结果"))}</p><p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">{textValue(row.text, textValue(row.excerpt))}</p></div><div className="text-right font-mono text-[10px] text-muted-foreground"><p>score {textValue(row.score)}</p><p>{textValue(row.route, textValue(row.retrieval_route))}</p></div></div>)}</div> : <EmptyState icon={ScanSearch} compact title="运行一次检索诊断" description="查看召回路由、融合得分、重排移动和最终证据。" />}
            </section>
          </TabsContent>
          <TabsContent value="migrations">
            <section className="overflow-hidden border border-border bg-surface">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3"><div><h3 className="text-[13px] font-semibold">索引代际迁移</h3><p className="text-[11px] text-muted-foreground">扫描、迁移、回切和验收均沿用现有后端状态机。</p></div><div className="flex gap-2"><Button onClick={() => void migrations.refetch()}><Gauge className="size-4" />重新扫描</Button><Button variant="primary" onClick={() => startMigration.mutate()} loading={startMigration.isPending}><Play className="size-4" />迁移当前知识库</Button></div></div>
              <QueryState pending={migrations.isPending} error={migrations.error} onRetry={() => void migrations.refetch()} />
              {migrationRows.length ? <div className="divide-y divide-border">{migrationRows.map((row, index) => <div key={textValue(row.kb_id, String(index))} className="grid grid-cols-[minmax(0,1fr)_160px_120px] items-center gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.kb_id, "知识库")}</p><p className="font-mono text-[10px] text-muted-foreground">{textValue(row.active_generation_id, textValue(row.current_generation, textValue(row.generation_id)))}</p></div><span className="text-xs text-muted-foreground">{Array.isArray(row.reasons) ? row.reasons.join("、") : textValue(row.reason, "索引契约检查")}</span><StatusBadge status={textValue(row.status, Boolean(row.needs_migration) ? "pending" : "ready")} /></div>)}</div> : migrations.data ? <EmptyState icon={Gauge} compact title="没有需要迁移的索引" description="当前知识库索引契约没有发现代际迁移项。" /> : null}
              {migrationRunId ? <div className="border-t border-border bg-surface-subtle p-4"><div className="flex flex-wrap items-center gap-3"><div className="min-w-0 flex-1"><p className="text-[13px] font-semibold">迁移运行 <span className="font-mono text-[10px] font-normal text-muted-foreground">{migrationRunId}</span></p><div className="mt-1 flex items-center gap-2"><StatusBadge status={runStatus || "pending"} /><span className="text-[11px] text-muted-foreground">{textValue(runRow.updated_at, textValue(runRow.created_at))}</span></div></div><Button onClick={() => void run.refetch()}>刷新进度</Button><Button onClick={() => migrationAction.mutate("rollback")} disabled={!['completed', 'completed_with_failures'].includes(runStatus)} loading={migrationAction.isPending}><RotateCcw className="size-4" />回滚旧代</Button><Button variant="primary" onClick={() => migrationAction.mutate("finalize")} disabled={runStatus !== "completed"} loading={migrationAction.isPending}><CheckCircle2 className="size-4" />验收并清理</Button></div>{run.data ? <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap border border-border bg-surface p-3 font-mono text-[10px] leading-4 text-muted-foreground">{JSON.stringify(run.data, null, 2)}</pre> : null}</div> : null}
            </section>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
