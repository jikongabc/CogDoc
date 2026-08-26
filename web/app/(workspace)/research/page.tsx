"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, CheckCircle2, FileSearch, FlaskConical, Pause, Pencil, Play, Plus, RefreshCw, Send, Square, WandSparkles } from "lucide-react";
import { useFieldArray, useForm } from "react-hook-form";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { z } from "zod";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord, type JsonValue } from "@/lib/api/control-plane";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace-store";

const createSchema = z.object({
  title: z.string().trim().max(160),
  objective: z.string().trim().min(8, "请具体描述研究目标").max(4000),
  sections: z.string().max(4000),
  isLocal: z.boolean(),
});
type CreateValues = z.infer<typeof createSchema>;

function CreateResearchDialog({ kbId, onCreated }: { kbId: string; onCreated: (jobId: string) => void }) {
  const [open, setOpen] = useState(false);
  const localModelMode = useWorkspaceStore((state) => state.localModelMode);
  const form = useForm<CreateValues>({ resolver: zodResolver(createSchema), defaultValues: { title: "", objective: "", sections: "", isLocal: localModelMode } });
  const mutation = useMutation({
    mutationFn: (values: CreateValues) => controlApi.createResearchJob({
      kb_id: kbId,
      title: values.title,
      objective: values.objective,
      section_titles: values.sections.split("\n").map((item) => item.trim()).filter(Boolean),
      is_local: values.isLocal,
    }),
    onSuccess: (value) => {
      const envelope = isRecord(value) ? value : {};
      const row = isRecord(envelope.job) ? envelope.job : envelope;
      toast.success("研究任务已创建");
      setOpen(false);
      form.reset();
      onCreated(textValue(row.job_id, ""));
    },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />新建研究</Button></DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader><DialogTitle>新建 Research</DialogTitle><DialogDescription>定义目标和初始章节。创建后可自动生成研究计划。</DialogDescription></DialogHeader>
        <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
          <div className="space-y-4">
            <div className="space-y-1.5"><Label htmlFor="research-title">标题</Label><Input id="research-title" placeholder="例如：企业知识治理调研" {...form.register("title")} /></div>
            <div className="space-y-1.5"><Label htmlFor="research-objective">研究目标</Label><Textarea id="research-objective" className="min-h-28" placeholder="说明要回答的问题、范围和交付标准" {...form.register("objective")} />{form.formState.errors.objective ? <p className="text-xs text-error">{form.formState.errors.objective.message}</p> : null}</div>
            <div className="space-y-1.5"><Label htmlFor="research-sections">初始章节（每行一个，可选）</Label><Textarea id="research-sections" placeholder={"现状与范围\n关键证据\n风险与建议"} {...form.register("sections")} /></div>
            <label className="flex items-start gap-2 text-[13px]"><input type="checkbox" className="mt-0.5 size-4 accent-primary" {...form.register("isLocal")} /><span><span className="block font-medium">优先使用本地模型</span><span className="block text-xs text-muted-foreground">沿用现有 Research 模型选择参数。</span></span></label>
            {form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-xs text-error">{form.formState.errors.root.message}</p> : null}
          </div>
          <DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>创建研究</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type ResearchDecision = "pending" | "approved" | "accepted_gap" | "changes_requested";
interface ResearchReviewValue { decision: ResearchDecision; note: string }

const planSectionSchema = z.object({
  title: z.string().trim().min(1, "章节标题不能为空").max(160),
  researchQuestion: z.string().trim().min(1, "研究问题不能为空").max(2000),
  requirements: z.string().trim().min(1, "至少需要一条证据需求"),
  retrievalQueries: z.string().trim().min(1, "主检索表达不能为空"),
  recoveryQueries: z.string().trim().min(1, "恢复检索表达不能为空"),
  successCriteria: z.string().trim().max(1000),
});
const planSchema = z.object({ sections: z.array(planSectionSchema).min(1).max(12) }).superRefine((value, context) => {
  value.sections.forEach((section, index) => {
    const [requirements, retrievalQueries, recoveryQueries] = [section.requirements, section.retrievalQueries, section.recoveryQueries].map((text) => text.split("\n").map((item) => item.trim()).filter(Boolean)) as [string[], string[], string[]];
    if (requirements.length > 3) context.addIssue({ code: "custom", path: ["sections", index, "requirements"], message: "每个章节最多 3 条证据需求" });
    if ([retrievalQueries, recoveryQueries].some((group) => group.length !== requirements.length)) context.addIssue({ code: "custom", path: ["sections", index, "requirements"], message: "证据需求、主检索和恢复检索必须逐行对应" });
    retrievalQueries.forEach((query, queryIndex) => {
      if (query.toLocaleLowerCase() === recoveryQueries[queryIndex]?.toLocaleLowerCase()) context.addIssue({ code: "custom", path: ["sections", index, "recoveryQueries"], message: `第 ${queryIndex + 1} 条恢复检索不能与主检索相同` });
    });
  });
});
type PlanValues = z.infer<typeof planSchema>;

function planLines(value: JsonValue | undefined, key: string) {
  if (!Array.isArray(value)) return "";
  return value.flatMap((item) => isRecord(item) ? [textValue(item[key], "")] : []).filter(Boolean).join("\n");
}

function EditResearchPlanDialog({ jobId, revision, sections, onSaved }: { jobId: string; revision: number; sections: JsonRecord[]; onSaved: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const form = useForm<PlanValues>({
    resolver: zodResolver(planSchema),
    defaultValues: { sections: sections.map((section) => ({
      title: textValue(section.title, ""),
      researchQuestion: textValue(section.research_question, textValue(section.objective, "")),
      requirements: planLines(section.evidence_requirements, "question"),
      retrievalQueries: planLines(section.evidence_requirements, "retrieval_query"),
      recoveryQueries: planLines(section.evidence_requirements, "recovery_query"),
      successCriteria: textValue(section.success_criteria, ""),
    })) },
  });
  const fields = useFieldArray({ control: form.control, name: "sections" });
  const mutation = useMutation({
    mutationFn: (values: PlanValues) => controlApi.updateResearchPlan(jobId, revision, values.sections.map((section) => {
      const questions = section.requirements.split("\n").map((item) => item.trim()).filter(Boolean);
      const retrieval = section.retrievalQueries.split("\n").map((item) => item.trim()).filter(Boolean);
      const recovery = section.recoveryQueries.split("\n").map((item) => item.trim()).filter(Boolean);
      return {
        title: section.title,
        research_question: section.researchQuestion,
        evidence_requirements: questions.map((question, index) => ({ question, retrieval_query: retrieval[index], recovery_query: recovery[index] })),
        success_criteria: section.successCriteria,
      };
    })),
    onSuccess: async () => { toast.success("研究计划已更新"); setOpen(false); await onSaved(); },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button size="compact"><Pencil className="size-3.5" />编辑计划</Button></DialogTrigger><DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl"><DialogHeader><DialogTitle>编辑研究计划</DialogTitle><DialogDescription>计划开始执行后将锁定。三组证据字段必须逐行对应，每章最多三条。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="divide-y divide-border border border-border">{fields.fields.map((field, index) => <section key={field.id} className="space-y-3 p-4"><div className="flex items-center justify-between"><p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">章节 {index + 1}</p>{fields.fields.length > 1 ? <Button type="button" variant="ghost" size="compact" onClick={() => fields.remove(index)}>移除章节</Button> : null}</div><div className="space-y-1.5"><Label htmlFor={`plan-title-${index}`}>标题</Label><Input id={`plan-title-${index}`} {...form.register(`sections.${index}.title`)} /></div><div className="space-y-1.5"><Label htmlFor={`plan-question-${index}`}>可验证研究问题</Label><Textarea id={`plan-question-${index}`} {...form.register(`sections.${index}.researchQuestion`)} /></div><div className="grid gap-3 lg:grid-cols-3"><div className="space-y-1.5"><Label htmlFor={`plan-requirements-${index}`}>原子证据需求</Label><Textarea id={`plan-requirements-${index}`} className="min-h-28" placeholder="每行一条" {...form.register(`sections.${index}.requirements`)} /></div><div className="space-y-1.5"><Label htmlFor={`plan-retrieval-${index}`}>主检索表达</Label><Textarea id={`plan-retrieval-${index}`} className="min-h-28" placeholder="与需求逐行对应" {...form.register(`sections.${index}.retrievalQueries`)} /></div><div className="space-y-1.5"><Label htmlFor={`plan-recovery-${index}`}>恢复检索表达</Label><Textarea id={`plan-recovery-${index}`} className="min-h-28" placeholder="措辞需与主检索不同" {...form.register(`sections.${index}.recoveryQueries`)} /></div></div>{form.formState.errors.sections?.[index] ? <p className="text-xs text-error">{form.formState.errors.sections[index]?.requirements?.message || form.formState.errors.sections[index]?.recoveryQueries?.message || form.formState.errors.sections[index]?.title?.message || form.formState.errors.sections[index]?.researchQuestion?.message}</p> : null}<div className="space-y-1.5"><Label htmlFor={`plan-success-${index}`}>完成标准</Label><Textarea id={`plan-success-${index}`} {...form.register(`sections.${index}.successCriteria`)} /></div></section>)}</div><div className="mt-3"><Button type="button" variant="ghost" size="compact" disabled={fields.fields.length >= 12} onClick={() => fields.append({ title: "", researchQuestion: "", requirements: "", retrievalQueries: "", recoveryQueries: "", successCriteria: "" })}><Plus className="size-3.5" />添加章节</Button></div>{form.formState.errors.root ? <p className="mt-3 text-xs text-error">{form.formState.errors.root.message}</p> : null}<DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>保存计划修订</Button></DialogFooter></form></DialogContent></Dialog>;
}

function ResearchReview({ jobId, revision, sections, onSaved }: { jobId: string; revision: number; sections: JsonRecord[]; onSaved: () => Promise<void> }) {
  const reviewKey = `${jobId}:${revision}`;
  const seed = useMemo(() => Object.fromEntries(sections.map((section, index) => {
    const id = textValue(section.section_id, `section-${index + 1}`);
    const current = textValue(section.review_status, "pending") as ResearchDecision;
    return [id, { decision: ["pending", "approved", "accepted_gap", "changes_requested"].includes(current) ? current : "pending", note: textValue(section.review_note, "") } satisfies ResearchReviewValue];
  })), [sections]);
  const [draft, setDraft] = useState<{ key: string; values: Record<string, ResearchReviewValue> }>({ key: "", values: {} });
  const values = draft.key === reviewKey ? draft.values : seed;
  const update = (id: string, patch: Partial<ResearchReviewValue>) => setDraft((current) => {
    const baseValues = current.key === reviewKey ? current.values : seed;
    const base = baseValues[id];
    if (!base) return { key: reviewKey, values: baseValues };
    return { key: reviewKey, values: { ...baseValues, [id]: { ...base, ...patch } } };
  });
  const mutation = useMutation({
    mutationFn: async () => {
      const decisions: JsonValue[] = sections.flatMap((section, index) => {
        const id = textValue(section.section_id, `section-${index + 1}`);
        const value = values[id];
        if (!value || value.decision === "pending") return [];
        if (["accepted_gap", "changes_requested"].includes(value.decision) && !value.note.trim()) throw new Error(`${textValue(section.title, id)}：接受缺口或退回修订时必须填写意见`);
        return [{ section_id: id, decision: value.decision, note: value.note.trim() }];
      });
      if (!decisions.length) throw new Error("请至少选择一个审阅决定");
      return controlApi.reviewResearch(jobId, revision, decisions);
    },
    onSuccess: async () => { toast.success("研究审阅决定已保存"); await onSaved(); },
    onError: (error) => toast.error(error.message),
  });
  if (!sections.length) return <EmptyState icon={CheckCircle2} compact title="没有可审阅章节" description="生成研究计划与报告后，可逐章批准、接受证据缺口或退回修订。" />;
  return <div className="space-y-3 p-5"><div className="border-l-2 border-primary bg-primary-subtle px-3 py-2 text-xs leading-5 text-muted-foreground">逐章决定会进入 Research 后端状态机。接受证据缺口和退回修订必须留下审阅意见。</div><div className="divide-y divide-border border border-border">{sections.map((section, index) => { const id = textValue(section.section_id, `section-${index + 1}`); const value = values[id]; if (!value) return null; const generated = textValue(section.generation_status, "generated") === "generated"; return <div key={id} className="grid gap-3 p-4 lg:grid-cols-[minmax(180px,0.8fr)_190px_minmax(240px,1.2fr)]"><div><p className="text-[13px] font-medium">{textValue(section.title, `章节 ${index + 1}`)}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground">{id}</p></div><Select value={value.decision} onValueChange={(decision) => update(id, { decision: decision as ResearchDecision })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="pending">暂不处理</SelectItem>{generated ? <SelectItem value="approved">批准正文</SelectItem> : <SelectItem value="accepted_gap">接受证据缺口</SelectItem>}<SelectItem value="changes_requested">退回修订</SelectItem></SelectContent></Select><Textarea value={value.note} onChange={(event) => update(id, { note: event.target.value })} placeholder="记录审阅意见" className="min-h-16" /></div>; })}</div><div className="flex justify-end"><Button variant="primary" onClick={() => mutation.mutate()} loading={mutation.isPending}><CheckCircle2 className="size-4" />保存审阅决定</Button></div></div>;
}

function ResearchDetail({ jobId, onRefresh }: { jobId: string; onRefresh: () => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["research", "job", jobId],
    queryFn: () => controlApi.researchJob(jobId),
    refetchInterval: (query) => {
      const envelope = isRecord(query.state.data) ? query.state.data : {};
      const row = isRecord(envelope.job) ? envelope.job : envelope;
      return ["pending", "running", "generating"].includes(textValue(row.status, "").toLowerCase()) ? 3000 : false;
    },
  });
  const detailEnvelope = isRecord(detail.data) ? detail.data : {};
  const row = isRecord(detailEnvelope.job) ? detailEnvelope.job : detailEnvelope;
  const status = textValue(row.status, "unknown").toLowerCase();
  const reviewStatus = textValue(row.review_status, "pending").toLowerCase();
  const report = useQuery({
    queryKey: ["research", "report", jobId],
    queryFn: () => controlApi.researchReport(jobId),
    enabled: status === "completed",
    retry: false,
  });
  const provenance = useQuery({ queryKey: ["research", "provenance", jobId], queryFn: () => controlApi.researchProvenance(jobId), retry: false });
  const revision = numberValue(row.revision);
  const plan = isRecord(row.plan) ? row.plan : row;
  const sections = records(plan, ["sections"]);
  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["research"] });
    onRefresh();
  };
  const action = useMutation({
    mutationFn: (name: string) => controlApi.researchAction(jobId, name),
    onSuccess: async () => { toast.success("研究状态已更新"); await invalidate(); },
    onError: (error) => toast.error(error.message.includes("provenance is stale") ? "知识库索引已更新，请先重新取证，再生成报告" : error.message),
  });
  const generate = useMutation({
    mutationFn: () => controlApi.generateResearchPlan(jobId, revision, row.is_local === true || row.is_local === "true"),
    onSuccess: async () => { toast.success("研究计划已生成"); await invalidate(); },
    onError: (error) => toast.error(error.message),
  });
  const publish = useMutation({
    mutationFn: () => controlApi.publishResearch(jobId, revision),
    onSuccess: async () => { toast.success("研究报告已发布"); await invalidate(); },
    onError: (error) => toast.error(error.message),
  });
  if (detail.isPending || detail.isError) return <QueryState pending={detail.isPending} error={detail.error} onRetry={() => void detail.refetch()} label="正在读取研究任务" />;
  const reportText = typeof report.data === "string" ? report.data : "";
  const provenanceRow = isRecord(provenance.data) ? provenance.data : {};
  const capturedProvenance = isRecord(provenanceRow.captured) ? provenanceRow.captured : {};
  const provenanceRows = records(capturedProvenance, ["source_versions"]);
  const downloadReport = () => {
    const url = URL.createObjectURL(new Blob([reportText], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${jobId}.md`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="min-w-0">
      <div className="flex min-h-[76px] items-start justify-between gap-4 border-b border-border px-5 py-3.5">
        <div className="min-w-0"><div className="flex items-center gap-2"><StatusBadge status={status} /><span className="font-mono text-[10px] text-muted-foreground">r{revision}</span></div><h3 className="mt-1.5 truncate text-lg font-semibold">{textValue(row.title, "未命名研究")}</h3><p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{textValue(row.objective, "未提供研究目标")}</p></div>
        <div className="flex shrink-0 items-center gap-1.5">
          {["draft", "pending"].includes(status) ? <Button size="compact" onClick={() => generate.mutate()} loading={generate.isPending}><WandSparkles className="size-3.5" />生成计划</Button> : null}
          {["draft", "pending", "planned"].includes(status) ? <Button variant="primary" size="compact" onClick={() => action.mutate("start")} loading={action.isPending}><Play className="size-3.5" />开始</Button> : null}
          {status === "running" ? <Button size="compact" onClick={() => action.mutate("pause")}><Pause className="size-3.5" />暂停</Button> : null}
          {status === "paused" ? <Button variant="primary" size="compact" onClick={() => action.mutate("resume")}><Play className="size-3.5" />继续</Button> : null}
          {status === "evidence_ready" ? <><Button size="compact" onClick={() => action.mutate("refresh")} loading={action.isPending}><RefreshCw className="size-3.5" />重新取证</Button><Button variant="primary" size="compact" onClick={() => action.mutate("generate")} loading={action.isPending}><WandSparkles className="size-3.5" />生成报告</Button></> : null}
          {["pending", "running", "paused", "generating"].includes(status) ? <Button variant="ghost" size="compact" onClick={() => action.mutate("cancel")}><Square className="size-3.5" />取消</Button> : null}
          {status === "completed" && reviewStatus === "approved" && !textValue(row.published_at, "") ? <Button variant="primary" size="compact" onClick={() => publish.mutate()} loading={publish.isPending}><Send className="size-3.5" />发布</Button> : null}
        </div>
      </div>
      <Tabs defaultValue="plan">
        <TabsList className="h-10 gap-6 px-5"><TabsTrigger value="plan">计划</TabsTrigger><TabsTrigger value="report">报告</TabsTrigger><TabsTrigger value="review">审阅</TabsTrigger><TabsTrigger value="provenance">证据链</TabsTrigger><TabsTrigger value="activity">运行状态</TabsTrigger></TabsList>
        <TabsContent value="plan" className="p-5">{sections.length ? <><div className="mb-3 flex items-center justify-between gap-3"><p className="text-xs text-muted-foreground">执行前可修订章节、研究问题和逐条检索表达。</p>{status === "planned" ? <EditResearchPlanDialog jobId={jobId} revision={revision} sections={sections} onSaved={invalidate} /> : null}</div><div className="border border-border">{sections.map((section, index) => <div key={textValue(section.section_id, String(index))} className="border-b border-border p-3 last:border-b-0"><div className="flex items-start gap-3"><span className="mt-0.5 font-mono text-[10px] text-muted-foreground">{String(index + 1).padStart(2, "0")}</span><div><p className="text-[13px] font-medium">{textValue(section.title, `章节 ${index + 1}`)}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{textValue(section.research_question, textValue(section.objective, textValue(section.description, "等待定义证据要求")))}</p><p className="mt-1 text-[11px] text-muted-foreground">{Array.isArray(section.evidence_requirements) ? `${section.evidence_requirements.length} 条证据需求` : "尚未定义证据需求"}</p></div></div></div>)}</div></> : <EmptyState icon={FileSearch} compact title="尚未生成研究计划" description="生成计划后可检查章节和原子证据要求。" action={<Button onClick={() => generate.mutate()} loading={generate.isPending}><WandSparkles className="size-4" />生成计划</Button>} />}</TabsContent>
        <TabsContent value="report" className="p-5">{reportText ? <><div className="mb-4 flex justify-end"><Button onClick={downloadReport}>下载 Markdown</Button></div><article className="answer-markdown mx-auto max-w-3xl text-[14px] leading-6"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{reportText}</ReactMarkdown></article></> : <EmptyState icon={FlaskConical} compact title="报告尚未生成" description="研究完成取证和章节生成后，报告会出现在这里供审阅。" />}</TabsContent>
        <TabsContent value="review"><ResearchReview jobId={jobId} revision={revision} sections={sections} onSaved={invalidate} /></TabsContent>
        <TabsContent value="provenance" className="p-5">{provenanceRows.length ? <div className="divide-y divide-border border border-border">{provenanceRows.map((item, index) => <div key={textValue(item.evidence_id, String(index))} className="grid grid-cols-[32px_minmax(0,1fr)_auto] gap-3 px-3 py-3"><span className="font-mono text-[10px] text-muted-foreground">[{index + 1}]</span><div className="min-w-0"><p className="truncate text-[13px] font-medium">{textValue(item.source, textValue(item.title, "证据来源"))}</p><p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{textValue(item.text, textValue(item.excerpt, textValue(item.requirement, "来源元数据已记录")))}</p></div><StatusBadge status={textValue(item.status, "verified")} /></div>)}</div> : <EmptyState icon={CheckCircle2} compact title="暂无证据链记录" description="Research 开始检索后，这里会显示要求、来源和核验状态。" />}</TabsContent>
        <TabsContent value="activity" className="p-5"><dl className="grid gap-px overflow-hidden border border-border bg-border sm:grid-cols-2">{[["任务 ID", jobId], ["知识库", textValue(row.kb_id)], ["当前状态", status], ["修订版本", revision], ["创建时间", textValue(row.created_at)], ["更新时间", textValue(row.updated_at)]].map(([label, value]) => <div key={String(label)} className="bg-surface px-3 py-2.5"><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="mt-0.5 truncate font-mono text-xs">{String(value)}</dd></div>)}</dl></TabsContent>
      </Tabs>
    </div>
  );
}

export default function ResearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const knowledgeBases = useKnowledgeBases();
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const requestedKbId = String(searchParams.get("kb") || "");
  const requestedJobId = String(searchParams.get("job") || "");
  const storedKbId = selectedKbId ?? "";
  const kbRows = knowledgeBases.data ?? [];
  const activeKb = kbRows.some((kb) => kb.kb_id === requestedKbId)
    ? requestedKbId
    : kbRows.some((kb) => kb.kb_id === storedKbId)
      ? storedKbId
      : kbRows[0]?.kb_id ?? "";
  const jobs = useQuery({ queryKey: ["research", "jobs", activeKb], queryFn: () => controlApi.researchJobs(activeKb), enabled: Boolean(activeKb), refetchInterval: 10_000 });
  const jobRows = useMemo(() => records(jobs.data, ["items", "jobs", "summaries"]), [jobs.data]);
  const activeJobId = jobRows.some((row) => textValue(row.job_id, "") === requestedJobId) ? requestedJobId : textValue(jobRows[0]?.job_id, "");
  useEffect(() => {
    if (activeKb && activeKb !== selectedKbId) setSelectedKbId(activeKb);
  }, [activeKb, selectedKbId, setSelectedKbId]);
  const selectKb = (kbId: string) => {
    setSelectedKbId(kbId);
    router.replace(`/research?kb=${encodeURIComponent(kbId)}`);
  };
  const selectJob = (jobId: string) => {
    router.replace(`/research?kb=${encodeURIComponent(activeKb)}&job=${encodeURIComponent(jobId)}`);
  };
  return (
    <div className="flex min-h-full flex-col">
      <PageHeader eyebrow="Evidence research" title="研究" description="从目标到计划、取证、审阅与发布，完整保留 Research 的可暂停、可恢复生命周期。" actions={activeKb ? <CreateResearchDialog kbId={activeKb} onCreated={(id) => { selectJob(id); void jobs.refetch(); }} /> : undefined} />
      <div className="flex h-12 items-center gap-3 border-b border-border bg-surface px-5"><Label htmlFor="research-kb" className="text-muted-foreground">知识库</Label><Select value={activeKb} onValueChange={selectKb}><SelectTrigger id="research-kb" className="w-64"><SelectValue placeholder="选择知识库" /></SelectTrigger><SelectContent>{kbRows.map((kb) => <SelectItem key={kb.kb_id} value={kb.kb_id}>{kb.kb_id}</SelectItem>)}</SelectContent></Select>{activeKb ? <Badge>{jobRows.length} 个任务</Badge> : null}<Button variant="ghost" size="icon" className="ml-auto" onClick={() => void jobs.refetch()} aria-label="刷新研究任务"><RefreshCw className={cn("size-4", jobs.isFetching && "animate-spin")} /></Button></div>
      {!knowledgeBases.isPending && !activeKb ? <EmptyState icon={FlaskConical} title="先创建知识库" description="Research 必须绑定一个知识库和固定的权限边界。" /> : <div className="grid min-h-0 flex-1 xl:grid-cols-[300px_minmax(0,1fr)]"><aside className="min-h-0 border-r border-border bg-surface xl:overflow-auto"><QueryState pending={jobs.isPending} error={jobs.error} onRetry={() => void jobs.refetch()} label="正在读取研究队列" />{jobs.data && !jobRows.length ? <EmptyState icon={FlaskConical} compact title="没有研究任务" description="新建研究，定义目标并生成可执行计划。" /> : null}<div className="divide-y divide-border">{jobRows.map((job) => { const id = textValue(job.job_id, ""); return <button key={id} onClick={() => selectJob(id)} className={cn("w-full px-4 py-3 text-left hover:bg-surface-subtle", activeJobId === id && "bg-primary-subtle")}><div className="flex items-center justify-between gap-2"><p className="truncate text-[13px] font-medium">{textValue(job.title, "未命名研究")}</p><StatusBadge status={textValue(job.status, "draft")} /></div><p className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted-foreground">{textValue(job.objective, textValue(job.objective_preview, "未提供目标"))}</p><p className="mt-2 font-mono text-[10px] text-muted-foreground">{id.slice(0, 18)}</p></button>; })}</div></aside><section className="min-w-0 bg-surface">{activeJobId ? <ResearchDetail jobId={activeJobId} onRefresh={() => void jobs.refetch()} /> : <EmptyState icon={Bot} title="选择研究任务" description="检查计划、运行状态、报告和完整证据链。" />}</section></div>}
    </div>
  );
}
