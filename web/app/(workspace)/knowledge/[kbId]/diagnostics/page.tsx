"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CheckCircle2, Gauge, Minus, Play, RotateCcw, Save, ScanSearch } from "lucide-react";
import { useParams, useSearchParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { TraceDebugger } from "@/components/diagnostics/trace-debugger";
import { RagEvaluation } from "@/components/diagnostics/rag-evaluation";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePermission } from "@/features/auth/permissions";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { decodeRouteParam } from "@/lib/routing";

interface RetrievalForm {
  query: string;
  topK: number;
  rerank: boolean;
}

type EvaluationChoice = "skip" | "gold" | "negative";

function evaluationEvidence(row: JsonRecord) {
  return {
    chunk_id: textValue(row.chunk_id, ""),
    source: textValue(row.source, ""),
    source_sha256: textValue(row.source_sha256, ""),
    parent_chunk_id: textValue(row.parent_chunk_id, ""),
  };
}

function RetrievalDiagnosticsResult({ value, kbId, onSaved }: { value: JsonRecord; kbId: string; onSaved: () => void }) {
  const queryClient = useQueryClient();
  const hits = records(value, ["final", "hits", "items", "results"]);
  const routes = records(value, ["routes"]);
  const decision = isRecord(value.decision) ? value.decision : {};
  const latency = isRecord(value.latency_ms) ? value.latency_ms : {};
  const channelCounts = isRecord(value.channel_counts) ? value.channel_counts : {};
  const supported = decision.supported === true;
  const [choices, setChoices] = useState<Record<string, EvaluationChoice>>({});
  const [noAnswer, setNoAnswer] = useState(false);
  const saveDraft = useMutation({
    mutationFn: async () => {
      const query = textValue(value.query, "").trim();
      if (!query) throw new Error("诊断结果缺少检索问题，无法保存评测样本");
      const selected = hits.map((row) => ({ row, choice: choices[textValue(row.chunk_id, "")] ?? "skip" })).filter(({ choice }) => choice !== "skip");
      const incomplete = selected.find(({ row }) => {
        const evidence = evaluationEvidence(row);
        return !evidence.chunk_id || !evidence.source || !evidence.source_sha256;
      });
      if (incomplete) throw new Error("所选证据缺少来源指纹，请重新索引文档后再保存");
      const acceptable = selected.filter(({ choice }) => choice === "gold").map(({ row }) => evaluationEvidence(row));
      if (noAnswer && acceptable.length) throw new Error("应无答案样本不能同时包含正确证据");
      if (!noAnswer && !acceptable.length) throw new Error("请至少标记一条正确证据，或选择“应无答案”");
      return controlApi.saveRetrievalDiagnosticLabel({
        doc_id: kbId,
        query,
        requirement_id: "r1",
        requirement_label: query,
        no_answer: noAnswer,
        acceptable_evidence: noAnswer ? [] : acceptable,
        hard_negative_evidence: selected.filter(({ choice }) => choice === "negative").map(({ row }) => evaluationEvidence(row)),
      });
    },
    onSuccess: async () => {
      toast.success("已保存为 RAG 评测草稿");
      await queryClient.invalidateQueries({ queryKey: ["reviews", "retrieval", kbId] });
      onSaved();
    },
    onError: (error) => toast.error(error.message),
  });
  const setChoice = (chunkId: string, choice: EvaluationChoice) => setChoices((current) => ({ ...current, [chunkId]: current[chunkId] === choice ? "skip" : choice }));
  const setNoAnswerValue = (checked: boolean) => {
    setNoAnswer(checked);
    if (checked) setChoices((current) => Object.fromEntries(Object.entries(current).map(([key, choice]) => [key, choice === "gold" ? "skip" : choice])));
  };

  return (
    <div>
      <div className="grid gap-px border-b border-border bg-border sm:grid-cols-4">
        {[
          ["最终召回", String(hits.length)],
          ["参与路由", String(routes.length)],
          ["证据判断", supported ? "支持回答" : "证据不足"],
          ["总耗时", typeof latency.total === "number" ? `${latency.total.toFixed(0)} ms` : "—"],
        ].map(([label, metric]) => <div key={label} className="bg-surface px-4 py-3"><p className="text-[10px] text-muted-foreground">{label}</p><p className="mt-0.5 text-[13px] font-semibold tabular-nums">{metric}</p></div>)}
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-border bg-surface-subtle px-4 py-2 text-[10px] text-muted-foreground">
        <span>判断分数 {typeof decision.score === "number" ? decision.score.toFixed(3) : "—"}</span>
        <span>候选总数 {textValue(value.ranking_count, "0")}</span>
        {Object.entries(channelCounts).map(([channel, count]) => <span key={channel}>{channel} {String(count)}</span>)}
      </div>
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
        <div className="min-w-[240px] flex-1"><p className="text-xs font-semibold">建立评测样本</p><p className="mt-0.5 text-[11px] text-muted-foreground">标记正确证据和明显误召回，保存后进入 RAG 评测审核。</p></div>
        <label className="flex h-8 items-center gap-2 text-xs"><input type="checkbox" checked={noAnswer} onChange={(event) => setNoAnswerValue(event.target.checked)} className="size-4 accent-primary" />应无答案</label>
        <Button size="compact" variant="primary" loading={saveDraft.isPending} onClick={() => saveDraft.mutate()}><Save className="size-3.5" />保存到 RAG 评测</Button>
      </div>
      {hits.length ? <div className="divide-y divide-border">{hits.map((row, index) => {
        const retrieval = isRecord(row.retrieval) ? row.retrieval : {};
        const score = numberValue(retrieval.score, numberValue(retrieval.fused_score, numberValue(retrieval.rrf_score, Number.NaN)));
        const rankDelta = typeof row.rank_delta === "number" ? row.rank_delta : null;
        const chunkId = textValue(row.chunk_id, "");
        const identity = evaluationEvidence(row);
        const canLabel = Boolean(identity.chunk_id && identity.source && identity.source_sha256);
        const choice = choices[chunkId] ?? "skip";
        return <div key={chunkId || String(index)} className="grid gap-3 px-4 py-3 sm:grid-cols-[40px_minmax(0,1fr)_180px]"><span className="font-mono text-[10px] text-muted-foreground">#{textValue(row.rank, String(index + 1))}</span><div className="min-w-0"><p className="truncate text-[13px] font-medium">{textValue(row.source, "未知来源")}</p><p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">{textValue(row.text_preview, textValue(row.text, textValue(row.excerpt, "（无文本预览）")))}</p><p className="mt-1 truncate font-mono text-[9px] text-muted-foreground">{chunkId || "无 chunk id"}</p></div><div className="text-left font-mono text-[10px] text-muted-foreground sm:text-right"><p>score {Number.isFinite(score) ? score.toFixed(4) : "—"}</p><p>{rankDelta === null ? "未重排" : rankDelta > 0 ? `重排 ↑${rankDelta}` : rankDelta < 0 ? `重排 ↓${Math.abs(rankDelta)}` : "排名不变"}</p><p>{textValue(row.page_start, "—")}–{textValue(row.page_end, "—")} 页</p><div className="mt-2 flex justify-start gap-1 sm:justify-end"><Button type="button" size="compact" variant={choice === "gold" ? "secondary" : "ghost"} className={choice === "gold" ? "border-primary text-primary" : ""} disabled={!canLabel || noAnswer} aria-label={`将第 ${index + 1} 条标为正确证据`} onClick={() => setChoice(chunkId, "gold")}><Check className="size-3" />正确证据</Button><Button type="button" size="compact" variant={choice === "negative" ? "secondary" : "ghost"} className={choice === "negative" ? "border-warning text-warning" : ""} disabled={!canLabel} aria-label={`将第 ${index + 1} 条标为误召回`} onClick={() => setChoice(chunkId, "negative")}><Minus className="size-3" />误召回</Button></div></div></div>;
      })}</div> : <EmptyState icon={ScanSearch} compact title="诊断完成，未召回证据" description="当前查询在该知识库和用户可访问范围内没有命中。可调整问题、Top K 或关闭重排后再次运行。" />}
    </div>
  );
}

export default function DiagnosticsPage() {
  const params = useParams<{ kbId: string }>();
  const searchParams = useSearchParams();
  const kbId = decodeRouteParam(params.kbId);
  const canReview = usePermission("review");
  const [selectedTab, setSelectedTab] = useState<string | null>(null);
  const requestedTab = selectedTab ?? (searchParams.get("tab") === "rag" ? "rag" : "traces");
  const activeTab = requestedTab === "rag" && !canReview ? "traces" : requestedTab;
  const [migrationRunId, setMigrationRunId] = useState("");
  const retrievalForm = useForm<RetrievalForm>({ defaultValues: { query: "", topK: 12, rerank: true } });
  const diagnose = useMutation({
    mutationFn: (values: RetrievalForm) => controlApi.diagnoseRetrieval(kbId, values.query.trim(), Number(values.topK), values.rerank),
    onSuccess: (value) => toast.success(`检索诊断完成，返回 ${records(value, ["final", "hits", "items", "results"]).length} 条证据`),
    onError: (error) => toast.error(error.message),
  });
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
  const runRow = isRecord(run.data) ? run.data : {};
  const runStatus = textValue(runRow.status, "").toLowerCase();

  useEffect(() => {
    if (canReview || searchParams.get("tab") !== "rag") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("tab");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}`);
  }, [canReview, searchParams]);

  const changeTab = (value: string) => {
    setSelectedTab(value);
    const url = new URL(window.location.href);
    if (value === "rag") url.searchParams.set("tab", "rag");
    else url.searchParams.delete("tab");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}`);
  };
  const openRetrievalDiagnostics = (query?: string) => {
    changeTab("retrieval");
    if (!query) return;
    const values = retrievalForm.getValues();
    retrievalForm.setValue("query", query, { shouldDirty: true, shouldValidate: true });
    diagnose.reset();
    diagnose.mutate({ ...values, query });
  };

  return (
    <div className="min-h-full">
      <PageHeader eyebrow="Operator tools" title="诊断" description="检查 Trace、检索路径、索引代际和当前知识库的 RAG 评测数据。" />
      <div className="p-4 md:p-6">
        <Tabs value={activeTab} onValueChange={changeTab}>
          <TabsList className="mb-4"><TabsTrigger value="traces">Trace 调试</TabsTrigger><TabsTrigger value="retrieval">检索诊断</TabsTrigger><TabsTrigger value="migrations">索引代际</TabsTrigger>{canReview ? <TabsTrigger value="rag">RAG 评测</TabsTrigger> : null}</TabsList>
          <TabsContent value="traces"><TraceDebugger kbId={kbId} /></TabsContent>
          <TabsContent value="retrieval">
            <section className="border border-border bg-surface">
              <form className="flex flex-wrap items-end gap-3 border-b border-border p-4" onSubmit={retrievalForm.handleSubmit((values) => diagnose.mutate(values), () => toast.error("请填写有效的检索问题和 Top K"))}><div className="min-w-[260px] flex-1 space-y-1.5"><Label htmlFor="diagnostic-query">检索问题</Label><Input id="diagnostic-query" aria-invalid={Boolean(retrievalForm.formState.errors.query)} placeholder="输入需要检查的真实查询" {...retrievalForm.register("query", { required: "请输入检索问题", validate: (value) => Boolean(value.trim()) || "检索问题不能为空", onChange: () => diagnose.reset() })} />{retrievalForm.formState.errors.query ? <p className="text-[11px] text-error">{retrievalForm.formState.errors.query.message}</p> : null}</div><div className="w-24 space-y-1.5"><Label htmlFor="top-k">Top K</Label><Input id="top-k" type="number" aria-invalid={Boolean(retrievalForm.formState.errors.topK)} {...retrievalForm.register("topK", { valueAsNumber: true, required: true, min: 1, max: 50, onChange: () => diagnose.reset() })} /></div><label className="flex h-9 items-center gap-2 text-xs"><input type="checkbox" className="size-4 accent-primary" {...retrievalForm.register("rerank", { onChange: () => diagnose.reset() })} />启用重排</label><Button type="submit" variant="primary" loading={diagnose.isPending}><Play className="size-4" />{diagnose.isPending ? "诊断中…" : "运行诊断"}</Button></form>
              <QueryState pending={diagnose.isPending} error={diagnose.error} onRetry={() => void retrievalForm.handleSubmit((values) => diagnose.mutate(values))()} label="正在执行多路召回与重排" errorTitle="检索诊断失败" />
              {!diagnose.isPending && !diagnose.error && diagnose.data ? <RetrievalDiagnosticsResult key={diagnose.submittedAt} value={diagnosticRow} kbId={kbId} onSaved={() => changeTab("rag")} /> : null}
              {!diagnose.isPending && !diagnose.error && !diagnose.data ? <EmptyState icon={ScanSearch} compact title="运行一次检索诊断" description="查看召回路由、融合得分、重排移动和最终证据。" /> : null}
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
          {canReview ? <TabsContent value="rag"><RagEvaluation kbId={kbId} onOpenRetrievalDiagnostics={openRetrievalDiagnostics} /></TabsContent> : null}
        </Tabs>
      </div>
    </div>
  );
}
