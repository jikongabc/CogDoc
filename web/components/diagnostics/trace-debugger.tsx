"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Bug, ChevronRight, RefreshCw } from "lucide-react";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api/client";
import { controlApi, records } from "@/lib/api/control-plane";
import { cn } from "@/lib/utils";

const TRACE_NODE_LABELS: Record<string, string> = {
  "runtime.setup": "运行准备",
  intent_router: "意图路由",
  rewrite_node: "问题改写",
  verify_rewrite_node: "改写校验",
  retrieve_node: "召回检索",
  rerank_node: "重排",
  generate_node: "答案生成",
  citation_node: "引用校验",
  qa_subgraph: "问答流程",
  summary_subgraph: "摘要流程",
  compare_subgraph: "对比流程",
};

function stringValue(value: unknown, fallback = "—") {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function numberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function recordList(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
}

function stringList(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

function formatDuration(value: unknown) {
  const duration = numberValue(value);
  if (duration === null) return "—";
  return duration >= 1000 ? `${(duration / 1000).toFixed(1)} s` : `${Math.round(duration)} ms`;
}

function nodeKey(nodeName: string) {
  if (TRACE_NODE_LABELS[nodeName]) return nodeName;
  const tail = nodeName.split(".").at(-1) || nodeName;
  return tail.includes(":") ? (tail.split(":", 1)[0] || tail) : tail;
}

function pageLabel(evidence: Record<string, unknown>) {
  const start = evidence.page_start ?? evidence.page;
  const end = evidence.page_end ?? evidence.page;
  if (start === undefined && end === undefined) return "";
  if (start === end || end === undefined) return `P${stringValue(start, "")}`;
  if (start === undefined) return `P${stringValue(end, "")}`;
  return `P${stringValue(start, "")}-${stringValue(end, "")}`;
}

function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return <div><p className="mb-1.5 text-[11px] font-medium text-muted-foreground">{label}</p><pre className="max-h-72 overflow-auto whitespace-pre-wrap border border-border bg-surface-subtle p-3 font-mono text-[10px] leading-4 text-muted-foreground">{JSON.stringify(value, null, 2)}</pre></div>;
}

function TraceStep({ step, index }: { step: Record<string, unknown>; index: number }) {
  const originalName = stringValue(step.node_name, stringValue(step.name, `step-${index + 1}`));
  const key = nodeKey(originalName);
  const label = TRACE_NODE_LABELS[key] || key;
  const rewrites = stringList(step.rewritten_queries);
  const counts = recordValue(step.counts);
  const evidence = recordList(step.evidence);
  const metadata = [
    step.task_type !== undefined ? ["任务", stringValue(step.task_type)] : null,
    step.model ? ["模型", stringValue(step.model)] : null,
    step.retrieval_top_k !== null && step.retrieval_top_k !== undefined ? ["top_k", stringValue(step.retrieval_top_k)] : null,
    step.retrieval_top_k_used !== null && step.retrieval_top_k_used !== undefined ? ["实际 top_k", stringValue(step.retrieval_top_k_used)] : null,
    step.error_class ? ["错误", stringValue(step.error_class)] : null,
  ].filter((item): item is string[] => Boolean(item));
  const rewriteCount = numberValue(counts.rewritten_query_count);

  return (
    <details className="group border-b border-border last:border-b-0">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 outline-none hover:bg-surface-subtle focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/50 [&::-webkit-details-marker]:hidden">
        <span className="flex size-6 shrink-0 items-center justify-center rounded-[4px] border border-border bg-surface font-mono text-[10px] text-muted-foreground">{index + 1}</span>
        <span className="min-w-0 flex-1"><span className="block truncate text-[13px] font-medium">{label}</span>{label !== key ? <span className="block truncate font-mono text-[10px] text-muted-foreground">{key}</span> : null}</span>
        <span className="font-mono text-[10px] text-muted-foreground">{formatDuration(step.duration_ms)}</span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
      </summary>
      <div className="relative ml-7 border-l border-border px-5 pb-5 pt-1">
        <p className="mb-3 font-mono text-[10px] text-muted-foreground">原始节点：{originalName}</p>
        {metadata.length ? <div className="mb-4 flex flex-wrap gap-1.5">{metadata.map(([name, value]) => <Badge key={name} variant={name === "错误" ? "error" : "neutral"}><span className="text-muted-foreground">{name}</span><span>{value}</span></Badge>)}</div> : null}
        <div className="space-y-4">
          {step.router_reason ? <div><p className="text-[11px] font-medium text-muted-foreground">路由理由</p><p className="mt-1 text-[13px] leading-5">{stringValue(step.router_reason)}</p></div> : null}
          {rewrites.length ? <div><p className="text-[11px] font-medium text-muted-foreground">改写查询</p><ol className="mt-1 space-y-1">{rewrites.map((query, rewriteIndex) => <li key={`${rewriteIndex}:${query}`} className="grid grid-cols-[20px_minmax(0,1fr)] text-[13px] leading-5"><span className="font-mono text-[10px] text-muted-foreground">{rewriteIndex + 1}</span><span>{query}</span></li>)}</ol></div> : rewriteCount ? <p className="text-xs text-muted-foreground">此 Trace 仅记录了 {rewriteCount} 条改写查询，未保存具体文本。</p> : null}
          {step.critique ? <div className="border-l-2 border-warning bg-warning-subtle px-3 py-2"><p className="text-[11px] font-medium text-warning">校验反馈</p><p className="mt-1 text-[13px] leading-5">{stringValue(step.critique)}</p></div> : null}
          {Object.keys(counts).length ? <JsonBlock value={counts} label="计数" /> : null}
          {evidence.length ? <div><p className="mb-1.5 text-[11px] font-medium text-muted-foreground">证据预览</p><div className="divide-y divide-border border border-border">{evidence.map((item, evidenceIndex) => <div key={`${stringValue(item.chunk_id, "evidence")}:${evidenceIndex}`} className="px-3 py-2.5"><div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="text-[12px] font-medium">{stringValue(item.source, stringValue(item.section_title, "派生知识"))}</span>{pageLabel(item) ? <Badge>{pageLabel(item)}</Badge> : null}<span className="font-mono text-[10px] text-muted-foreground">{stringValue(item.chunk_id, "")}</span></div><p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{stringValue(item.text_preview, "未保存证据预览")}</p></div>)}</div></div> : null}
          <details><summary className="cursor-pointer text-[11px] font-medium text-muted-foreground hover:text-foreground">原始节点数据</summary><div className="mt-2"><JsonBlock value={step} label="完整 JSON" /></div></details>
        </div>
      </div>
    </details>
  );
}

export function TraceDebugger({ kbId }: { kbId: string }) {
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [taskFilter, setTaskFilter] = useState("all");
  const traces = useQuery({ queryKey: ["diagnostics", "traces", kbId], queryFn: () => controlApi.traces(kbId) });
  const traceRows = records(traces.data, ["traces", "items"]);
  const statuses = useMemo(() => Array.from(new Set(traceRows.map((row) => stringValue(row.status, "unknown")))).sort(), [traceRows]);
  const tasks = useMemo(() => Array.from(new Set(traceRows.map((row) => stringValue(row.task_type, "unknown")))).sort(), [traceRows]);
  const filteredRows = traceRows.filter((row) => (statusFilter === "all" || stringValue(row.status) === statusFilter) && (taskFilter === "all" || stringValue(row.task_type) === taskFilter));
  const activeTraceId = filteredRows.some((row) => stringValue(row.trace_id, "") === selectedTraceId) ? selectedTraceId : stringValue(filteredRows[0]?.trace_id, "");
  const activeRow = filteredRows.find((row) => stringValue(row.trace_id, "") === activeTraceId);
  const trace = useQuery({ queryKey: ["diagnostics", "trace", activeTraceId], queryFn: () => api.trace(activeTraceId), enabled: Boolean(activeTraceId) });
  const traceSteps = trace.data?.steps ?? [];
  const traceSummary = trace.data?.summary;
  const refresh = async () => {
    await traces.refetch();
    if (activeTraceId) await trace.refetch();
  };

  return (
    <div className="grid min-h-[560px] overflow-hidden border border-border bg-surface xl:grid-cols-[340px_minmax(0,1fr)]">
      <section className="border-b border-border xl:border-b-0 xl:border-r">
        <div className="grid grid-cols-2 gap-2 border-b border-border p-3">
          <Select value={statusFilter} onValueChange={setStatusFilter}><SelectTrigger aria-label="Trace 状态"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem>{statuses.map((status) => <SelectItem key={status} value={status}>{status}</SelectItem>)}</SelectContent></Select>
          <Select value={taskFilter} onValueChange={setTaskFilter}><SelectTrigger aria-label="Trace 任务"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部任务</SelectItem>{tasks.map((task) => <SelectItem key={task} value={task}>{task}</SelectItem>)}</SelectContent></Select>
          <div className="col-span-2 flex items-center justify-between"><span className="text-[11px] text-muted-foreground">{filteredRows.length} / {traceRows.length} 条</span><Button variant="ghost" size="compact" onClick={() => void refresh()} loading={traces.isFetching || trace.isFetching}><RefreshCw className="size-3.5" />刷新</Button></div>
        </div>
        <QueryState pending={traces.isPending} error={traces.error} onRetry={() => void traces.refetch()} />
        {traces.data && !traceRows.length ? <EmptyState icon={Activity} compact title="没有 Trace" description="执行对话或检索后，可在这里检查节点、耗时和错误。" /> : filteredRows.length ? <div className="max-h-[660px] divide-y divide-border overflow-y-auto">{filteredRows.map((row) => { const id = stringValue(row.trace_id, ""); const active = id === activeTraceId; return <button key={id} type="button" onClick={() => setSelectedTraceId(id)} className={cn("w-full px-3 py-3 text-left hover:bg-surface-subtle", active && "bg-primary-subtle")} aria-pressed={active}><div className="flex items-start justify-between gap-2"><p className="line-clamp-2 text-[13px] font-medium leading-5">{stringValue(row.query_preview, "未记录问题")}</p><StatusBadge status={stringValue(row.status, "unknown")} /></div><div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-foreground"><span>{stringValue(row.task_type, "unknown")}</span><span>{formatDuration(row.duration_ms)}</span><span className="ml-auto font-mono">{id.slice(0, 12)}</span></div></button>; })}</div> : traces.data ? <EmptyState icon={Activity} compact title="没有匹配的 Trace" description="调整状态或任务筛选条件。" /> : null}
      </section>
      <section className="min-w-0">
        <QueryState pending={trace.isPending} error={trace.error} onRetry={() => void trace.refetch()} />
        {trace.data ? <>
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3"><div className="min-w-0"><h3 className="text-[14px] font-semibold">{stringValue(activeRow?.query_preview, "未记录问题")}</h3><p className="mt-1 font-mono text-[10px] text-muted-foreground">trace_id: {trace.data.trace_id} · request_id: {trace.data.request_id}</p></div><Button variant="ghost" size="compact" onClick={() => void trace.refetch()} loading={trace.isFetching}><RefreshCw className="size-3.5" />刷新 Trace</Button></div>
          <div className="grid gap-px border-b border-border bg-border sm:grid-cols-5">{[["状态", trace.data.status], ["任务", trace.data.task_type], ["耗时", formatDuration(trace.data.duration_ms)], ["步骤", traceSummary?.step_count ?? traceSteps.length], ["证据完整度", trace.data.evidence_completeness === null || trace.data.evidence_completeness === undefined ? "—" : `${Math.round(trace.data.evidence_completeness * 100)}%`]].map(([label, value]) => <div key={String(label)} className="bg-surface px-3 py-2.5"><p className="text-[10px] text-muted-foreground">{label}</p><p className="mt-0.5 truncate text-xs font-medium">{String(value)}</p></div>)}</div>
          <div className="border-b border-border px-4 py-3"><details><summary className="cursor-pointer text-[12px] font-medium hover:text-primary">请求配置</summary><div className="mt-3"><JsonBlock value={trace.data.config ?? {}} label="Config" /></div></details></div>
          {trace.data.error && Object.keys(trace.data.error).length ? <div className="border-b border-error/30 bg-error-subtle px-4 py-3"><details open><summary className="cursor-pointer text-[12px] font-semibold text-error">运行错误</summary><div className="mt-3"><JsonBlock value={trace.data.error} label="Error" /></div></details></div> : null}
          {traceSteps.length ? <div>{traceSteps.map((step, index) => <TraceStep key={`${stringValue(step.node_name, "step")}:${index}`} step={step} index={index} />)}</div> : <EmptyState icon={Bug} compact title="Trace 没有步骤" description="该记录未保存节点级执行信息。" />}
        </> : !activeTraceId && !trace.isPending ? <EmptyState icon={Bug} title="选择 Trace" description="查看请求配置、执行步骤和稳定错误信息。" /> : null}
      </section>
    </div>
  );
}
