"use client";

import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, BookMarked, Download, MessageSquareWarning, Pencil, Plus, RefreshCw, ScanSearch, ShieldCheck, Trash2 } from "lucide-react";
import { useParams } from "next/navigation";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { usePermission } from "@/features/auth/permissions";
import { useDocuments } from "@/features/knowledge/queries";
import { controlApi, isRecord, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { decodeRouteParam } from "@/lib/routing";

const schema = z.object({ text: z.string().trim().min(4).max(20000), certainty: z.enum(["low", "medium", "high"]), note: z.string().trim().max(1000), documentId: z.string() });

function CreateKnowledgeDialog({ kbId }: { kbId: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const documents = useDocuments(kbId);
  const documentRows = Array.isArray(documents.data) ? documents.data : [];
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { text: "", certainty: "medium", note: "", documentId: "unbound" } });
  const certainty = useWatch({ control: form.control, name: "certainty" });
  const documentId = useWatch({ control: form.control, name: "documentId" });
  const mutation = useMutation({
    mutationFn: (values: z.infer<typeof schema>) => {
      const relatedDocument = documentRows.find((document) => (document.document_id || document.name) === values.documentId);
      return controlApi.createKnowledge({
        kb_id: kbId,
        text: values.text,
        certainty: values.certainty,
        source_note: values.note,
        origin: "manual_entry",
        related_document_id: relatedDocument?.document_id || relatedDocument?.name,
        related_source: relatedDocument?.name,
        related_source_sha256: relatedDocument?.sha256 || undefined,
        related_chunk_ids: [],
      });
    },
    onSuccess: async () => { toast.success("派生知识已创建并进入审核"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["derived"] }); },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />新增知识</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>新增派生知识</DialogTitle><DialogDescription>补充内容会进入现有审核与索引流程，不会绕过证据治理。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor="knowledge-text">知识内容</Label><Textarea id="knowledge-text" className="min-h-36" {...form.register("text")} />{form.formState.errors.text ? <p className="text-xs text-error">{form.formState.errors.text.message}</p> : null}</div><div className="space-y-1.5"><Label htmlFor="related-document">关联文档</Label><Select value={documentId} onValueChange={(value) => form.setValue("documentId", value)} disabled={documents.isPending}><SelectTrigger id="related-document" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unbound">不关联文档</SelectItem>{documentRows.map((document) => { const id = document.document_id || document.name; return <SelectItem key={`${id}:${document.name}`} value={id}>{document.name}</SelectItem>; })}</SelectContent></Select>{documents.isPending ? <p className="text-xs text-muted-foreground">正在读取文档…</p> : documents.isError ? <p className="text-xs text-error">无法读取文档，请重试。</p> : !documentRows.length ? <p className="text-xs text-muted-foreground">当前知识库还没有可关联的文档。</p> : <p className="text-xs text-muted-foreground">关联后会保留文档 ID、文件名和内容摘要，用于权限校验与过期检测。</p>}</div><div className="space-y-1.5"><Label>确定性</Label><Select value={certainty} onValueChange={(value) => form.setValue("certainty", value as z.infer<typeof schema>["certainty"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["low", "medium", "high"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor="source-note">来源说明</Label><Textarea id="source-note" {...form.register("note")} /></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>保存为待审核</Button></DialogFooter></form></DialogContent></Dialog>;
}

function ReviseKnowledgeDialog({ kbId, row }: { kbId: string; row: JsonRecord }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const documents = useDocuments(kbId);
  const documentRows = Array.isArray(documents.data) ? documents.data : [];
  const currentDocumentId = textValue(row.related_document_id, "unbound");
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: {
      text: textValue(row.text, ""),
      certainty: (["low", "medium", "high"].includes(textValue(row.certainty, "medium")) ? textValue(row.certainty, "medium") : "medium") as z.infer<typeof schema>["certainty"],
      note: textValue(row.source_note, ""),
      documentId: currentDocumentId,
    },
  });
  const certainty = useWatch({ control: form.control, name: "certainty" });
  const documentId = useWatch({ control: form.control, name: "documentId" });
  const currentDocumentMissing = currentDocumentId !== "unbound" && !documentRows.some((document) => (document.document_id || document.name) === currentDocumentId);
  const mutation = useMutation({
    mutationFn: (values: z.infer<typeof schema>) => {
      const relatedDocument = documentRows.find((document) => (document.document_id || document.name) === values.documentId);
      return controlApi.reviseKnowledge(textValue(row.knowledge_id, textValue(row.id, "")), {
        text: values.text,
        certainty: values.certainty,
        source_note: values.note,
        ...(relatedDocument ? {
          related_document_id: relatedDocument.document_id || relatedDocument.name,
          related_source: relatedDocument.name,
          related_source_sha256: relatedDocument.sha256 || undefined,
          related_chunk_ids: [],
        } : {}),
      });
    },
    onSuccess: async () => {
      toast.success("修订版本已创建并进入审核");
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["derived"] });
    },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  if (!["approved", "stale"].includes(textValue(row.status, "pending"))) return null;
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="修订派生知识"><Pencil className="size-4" /></Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>{textValue(row.status) === "stale" ? "修订并重新绑定" : "修订派生知识"}</DialogTitle><DialogDescription>保存后创建新版本并重新进入审核，当前已发布版本不会被直接覆盖。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor={`revise-text-${textValue(row.knowledge_id, textValue(row.id))}`}>知识内容</Label><Textarea id={`revise-text-${textValue(row.knowledge_id, textValue(row.id))}`} className="min-h-36" {...form.register("text")} />{form.formState.errors.text ? <p className="text-xs text-error">{form.formState.errors.text.message}</p> : null}</div><div className="space-y-1.5"><Label>关联文档</Label><Select value={documentId} onValueChange={(value) => form.setValue("documentId", value)} disabled={documents.isPending}><SelectTrigger className="w-full" aria-label="修订关联文档"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unbound">保持未关联</SelectItem>{currentDocumentMissing ? <SelectItem value={currentDocumentId}>{textValue(row.related_source, currentDocumentId)}（当前绑定）</SelectItem> : null}{documentRows.map((document) => { const id = document.document_id || document.name; return <SelectItem key={`${id}:${document.name}`} value={id}>{document.name}</SelectItem>; })}</SelectContent></Select><p className="text-xs text-muted-foreground">选择新文档可重新绑定过期知识；不更改时保留当前来源关系。</p></div><div className="space-y-1.5"><Label>确定性</Label><Select value={certainty} onValueChange={(value) => form.setValue("certainty", value as z.infer<typeof schema>["certainty"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["low", "medium", "high"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label>来源说明</Label><Textarea {...form.register("note")} /></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>保存修订版本</Button></DialogFooter></form></DialogContent></Dialog>;
}

function ReviewKnowledgeDialog({ row }: { row: JsonRecord }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const queryClient = useQueryClient();
  const knowledgeId = textValue(row.knowledge_id, textValue(row.id, ""));
  const relatedSource = textValue(row.related_source, textValue(row.related_document_id, "未关联文档"));
  const mutation = useMutation({
    mutationFn: (action: "approve" | "reject") => controlApi.reviewKnowledge(knowledgeId, action, note.trim()),
    onSuccess: async (_, action) => {
      toast.success(action === "approve" ? "派生知识已通过" : "派生知识已拒绝");
      setOpen(false);
      setNote("");
      await queryClient.invalidateQueries({ queryKey: ["derived"] });
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="secondary" size="compact"><ShieldCheck className="size-3.5" />审核</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>审核派生知识</DialogTitle>
          <DialogDescription>对照关联文档判断内容是否准确。决定与审核意见会进入现有审计记录。</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="border-l-2 border-primary bg-primary-subtle px-4 py-3">
            <p className="text-[11px] font-medium text-primary">待审核内容</p>
            <p className="mt-1 whitespace-pre-wrap text-[13px] leading-6">{textValue(row.text)}</p>
          </div>
          <dl className="grid gap-px overflow-hidden border border-border bg-border sm:grid-cols-2">
            {[["关联文档", relatedSource], ["确定性", textValue(row.certainty, "medium")], ["来源", textValue(row.origin, "manual")], ["状态", textValue(row.status, "pending")]].map(([label, value]) => (
              <div key={label} className="bg-surface px-3 py-2.5"><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="mt-0.5 truncate text-xs">{value}</dd></div>
            ))}
          </dl>
          <div className="space-y-1.5">
            <Label htmlFor={`knowledge-review-note-${knowledgeId}`}>审核意见</Label>
            <Textarea id={`knowledge-review-note-${knowledgeId}`} value={note} onChange={(event) => setNote(event.target.value)} placeholder="记录通过或拒绝的依据，便于后续复核。" />
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="secondary" loading={mutation.isPending} onClick={() => mutation.mutate("reject")}>拒绝</Button>
          <Button type="button" variant="primary" loading={mutation.isPending} onClick={() => mutation.mutate("approve")}>通过并发布</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BatchReviewDialog({
  action,
  count,
  loading,
  onConfirm,
}: {
  action: "batch-approve" | "batch-reject";
  count: number;
  loading: boolean;
  onConfirm: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const approving = action === "batch-approve";
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={approving ? "primary" : "secondary"} size="compact">
          {approving ? "批量通过当前列表" : "批量拒绝当前列表"}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{approving ? "批量通过派生知识" : "批量拒绝派生知识"}</DialogTitle>
          <DialogDescription>
            将处理当前筛选结果中的 {count} 条记录。审核决定会写入审计记录，列表变化导致未处理的记录会单独提示。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button>
          <Button
            type="button"
            variant={approving ? "primary" : "secondary"}
            loading={loading}
            onClick={async () => {
              try {
                await onConfirm();
                setOpen(false);
              } catch {
                // The mutation owns the user-facing error toast; keep the
                // confirmation open so the reviewer can retry deliberately.
              }
            }}
          >
            确认处理 {count} 条
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function DerivedKnowledgePage() {
  const params = useParams<{ kbId: string }>();
  const kbId = decodeRouteParam(params.kbId);
  const queryClient = useQueryClient();
  const canReview = usePermission("review");
  const [status, setStatus] = useState("all");
  const knowledge = useQuery({ queryKey: ["derived", "knowledge", kbId, status], queryFn: () => controlApi.knowledge(kbId, status === "all" ? undefined : status) });
  const feedback = useQuery({ queryKey: ["derived", "feedback", kbId], queryFn: () => controlApi.feedback(kbId) });
  const analysis = useQuery({ queryKey: ["derived", "analysis", kbId], queryFn: () => controlApi.feedbackAnalysis(kbId) });
  const retrieval = useQuery({ queryKey: ["derived", "retrieval-feedback", kbId], queryFn: () => controlApi.retrievalFeedback(kbId) });
  const metrics = useQuery({ queryKey: ["derived", "metrics", kbId], queryFn: () => controlApi.feedbackLoopMetrics(kbId), retry: false });
  const pending = useQuery({ queryKey: ["derived", "pending", kbId], queryFn: () => controlApi.pendingKnowledge(kbId), retry: false });
  const indexStatus = useQuery({ queryKey: ["derived", "index-status", kbId], queryFn: () => controlApi.knowledgeIndexStatus(kbId), retry: false });
  const knowledgeRows = records(knowledge.data, ["items", "knowledge", "entries"]);
  const feedbackRows = records(feedback.data, ["items", "feedback"]);
  const analysisRows = records(analysis.data, ["items", "analysis"]);
  const retrievalRows = records(retrieval.data, ["items", "retrieval_feedback", "feedback"]);
  const remove = useMutation({ mutationFn: (row: JsonRecord) => controlApi.deleteKnowledge(textValue(row.knowledge_id, textValue(row.id, ""))), onSuccess: async () => { toast.success("派生知识已删除"); await queryClient.invalidateQueries({ queryKey: ["derived"] }); }, onError: (error) => toast.error(error.message) });
  const archive = useMutation({ mutationFn: (row: JsonRecord) => controlApi.reviewKnowledge(textValue(row.knowledge_id, textValue(row.id, "")), "archive", "从派生知识列表归档"), onSuccess: async () => { toast.success("派生知识已归档"); await queryClient.invalidateQueries({ queryKey: ["derived"] }); }, onError: (error) => toast.error(error.message) });
  const batchReview = useMutation({
    mutationFn: (action: "batch-approve" | "batch-reject") => controlApi.batchReviewKnowledge(knowledgeRows.map((row) => textValue(row.knowledge_id, textValue(row.id, ""))).filter(Boolean), action, "批量审核当前筛选结果"),
    onSuccess: async (value, action) => {
      const payload = isRecord(value) ? value : {};
      const missing = Array.isArray(payload.missing_ids) ? payload.missing_ids.length : 0;
      const updated = records(payload, ["updated"]).length;
      if (missing) {
        toast.warning(`已${action === "batch-approve" ? "通过" : "拒绝"} ${updated} 条，另有 ${missing} 条因列表变化未处理`);
      } else {
        toast.success(`已${action === "batch-approve" ? "通过" : "拒绝"} ${updated} 条派生知识`);
      }
      await queryClient.invalidateQueries({ queryKey: ["derived"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const scan = useMutation({ mutationFn: () => controlApi.scanStaleKnowledge(kbId), onSuccess: async () => { toast.success("过期扫描已完成"); await queryClient.invalidateQueries({ queryKey: ["derived"] }); }, onError: (error) => toast.error(error.message) });
  const exportQueue = useMutation({
    mutationFn: () => controlApi.reviewQueueExport(kbId),
    onSuccess: (value) => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `cogdoc-derived-knowledge-review-${kbId}.json`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success("派生知识审核队列已导出");
    },
    onError: (error) => toast.error(error.message),
  });
  const toggleRetrieval = useMutation({ mutationFn: (row: JsonRecord) => controlApi.setRetrievalFeedback(textValue(row.retrieval_feedback_id, textValue(row.feedback_id, textValue(row.id, ""))), !Boolean(row.enabled ?? true), "人工调整检索反馈状态"), onSuccess: async () => { toast.success("检索调权状态已更新"); await queryClient.invalidateQueries({ queryKey: ["derived", "retrieval-feedback"] }); }, onError: (error) => toast.error(error.message) });
  const metricPayload = isRecord(metrics.data) ? metrics.data : {};
  const metricCounts = isRecord(metricPayload.counts) ? metricPayload.counts : {};
  const pendingRow = isRecord(pending.data) ? pending.data : {};
  const indexRow = isRecord(indexStatus.data) ? indexStatus.data : {};
  const metricRow: JsonRecord = { pending_count: pendingRow.pending, approved_count: metricCounts.approved_knowledge_total ?? indexRow.approved_count, stale_count: pendingRow.stale, feedback_count: metricCounts.feedback_total };
  return <div className="min-h-full"><PageHeader eyebrow="Knowledge governance" title="派生知识" description="创建、修订并审核派生知识，同时管理反馈纠错、过期检测和检索调权。" actions={<>{canReview ? <Button onClick={() => exportQueue.mutate()} loading={exportQueue.isPending}><Download className="size-4" />导出审核队列</Button> : null}<Button onClick={() => scan.mutate()} loading={scan.isPending}><ScanSearch className="size-4" />扫描过期</Button><CreateKnowledgeDialog kbId={kbId} /></>} /><div className="p-4 md:p-6"><div className="mb-5 grid gap-px overflow-hidden border border-border bg-border sm:grid-cols-4">{[["待审核", textValue(metricRow.pending_count, "—")], ["已通过", textValue(metricRow.approved_count, "—")], ["需更新", textValue(metricRow.stale_count, "—")], ["反馈总数", textValue(metricRow.feedback_count, String(feedbackRows.length))]].map(([label, value]) => <div key={label} className="bg-surface px-4 py-3"><p className="text-[11px] text-muted-foreground">{label}</p><p className="mt-1 text-lg font-semibold">{value}</p></div>)}</div><Tabs defaultValue="knowledge"><TabsList className="mb-4"><TabsTrigger value="knowledge">知识与审核 <Badge className="ml-1">{knowledgeRows.length}</Badge></TabsTrigger><TabsTrigger value="feedback">用户反馈 <Badge className="ml-1">{feedbackRows.length}</Badge></TabsTrigger><TabsTrigger value="analysis">反馈理解 <Badge className="ml-1">{analysisRows.length}</Badge></TabsTrigger><TabsTrigger value="retrieval">检索调权 <Badge className="ml-1">{retrievalRows.length}</Badge></TabsTrigger></TabsList>
    <TabsContent value="knowledge"><section className="overflow-hidden border border-border bg-surface"><div className="flex min-h-11 flex-wrap items-center gap-3 border-b border-border px-4 py-2"><Label>状态</Label><Select value={status} onValueChange={setStatus}><SelectTrigger className="w-36"><SelectValue /></SelectTrigger><SelectContent>{["all", "pending", "approved", "rejected", "stale", "archived"].map((value) => <SelectItem key={value} value={value}>{value === "all" ? "全部" : value}</SelectItem>)}</SelectContent></Select>{canReview && knowledgeRows.length && ["pending", "stale"].includes(status) ? <><BatchReviewDialog action="batch-reject" count={knowledgeRows.length} loading={batchReview.isPending} onConfirm={() => batchReview.mutateAsync("batch-reject")} /><BatchReviewDialog action="batch-approve" count={knowledgeRows.length} loading={batchReview.isPending} onConfirm={() => batchReview.mutateAsync("batch-approve")} /></> : null}<Button variant="ghost" size="icon" className="ml-auto" onClick={() => void knowledge.refetch()}><RefreshCw className="size-4" /></Button></div><QueryState pending={knowledge.isPending} error={knowledge.error} onRetry={() => void knowledge.refetch()} />{knowledge.data && !knowledgeRows.length ? <EmptyState icon={BookMarked} compact title="没有派生知识" description="新增人工知识，或从反馈纠错中保存内容。" action={<CreateKnowledgeDialog kbId={kbId} />} /> : <div className="divide-y divide-border">{knowledgeRows.map((row) => { const id = textValue(row.knowledge_id, textValue(row.id)); const rowStatus = textValue(row.status, "pending"); const relatedSource = textValue(row.related_source, textValue(row.related_document_id, "")); return <div key={id} className="grid gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_100px_110px_auto] sm:items-start sm:gap-4"><div className="min-w-0"><p className="whitespace-pre-wrap text-[13px] leading-5">{textValue(row.text)}</p><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{id} · {textValue(row.origin, "manual")}{relatedSource ? ` · ${relatedSource}` : ""}</p></div><StatusBadge status={rowStatus} /><Badge className="w-fit">{textValue(row.certainty, "medium")}</Badge><div className="flex flex-wrap justify-end gap-1"><ReviseKnowledgeDialog kbId={kbId} row={row} />{canReview && ["pending", "stale"].includes(rowStatus) ? <ReviewKnowledgeDialog row={row} /> : null}{canReview && rowStatus === "approved" ? <Button variant="ghost" size="icon" loading={archive.isPending} onClick={() => archive.mutate(row)} aria-label="归档"><Archive className="size-4" /></Button> : null}<Button variant="ghost" size="icon" className="text-error" onClick={() => remove.mutate(row)} aria-label="删除"><Trash2 className="size-4" /></Button></div></div>; })}</div>}</section></TabsContent>
    <TabsContent value="feedback"><section className="overflow-hidden border border-border bg-surface">{feedbackRows.length ? <div className="divide-y divide-border">{feedbackRows.map((row, index) => <div key={textValue(row.feedback_id, String(index))} className="grid grid-cols-[100px_minmax(0,1fr)_160px] gap-4 px-4 py-3 text-[13px]"><StatusBadge status={textValue(row.feedback, "pending")} label={textValue(row.feedback, "反馈")} /><div><p className="font-medium">{textValue(row.feedback_type, "一般反馈")}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{textValue(row.comment, textValue(row.feedback_text, "未提供说明"))}</p></div><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.created_at)}</span></div>)}</div> : <EmptyState icon={MessageSquareWarning} compact title="没有用户反馈" description="对话中的好评、差评和纠错会出现在这里。" />}</section></TabsContent>
    <TabsContent value="analysis"><section className="overflow-hidden border border-border bg-surface">{analysisRows.length ? <div className="divide-y divide-border">{analysisRows.map((row, index) => <div key={textValue(row.feedback_id, String(index))} className="grid grid-cols-[minmax(0,1fr)_160px_100px] gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.recommended_action, "待分析")}</p><p className="mt-1 text-xs text-muted-foreground">{textValue(row.reason, textValue(row.summary))}</p></div><span>{textValue(row.feedback_type)}</span><StatusBadge status={Boolean(row.needs_review) ? "needs_review" : "completed"} /></div>)}</div> : <EmptyState icon={MessageSquareWarning} compact title="没有反馈分析" description="后端完成反馈理解后会显示建议动作和审核状态。" />}</section></TabsContent>
    <TabsContent value="retrieval"><section className="overflow-hidden border border-border bg-surface">{retrievalRows.length ? <div className="divide-y divide-border">{retrievalRows.map((row, index) => <div key={textValue(row.retrieval_feedback_id, textValue(row.feedback_id, String(index)))} className="grid grid-cols-[minmax(0,1fr)_120px_120px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.query, "检索反馈")}</p><p className="mt-1 text-xs text-muted-foreground">{textValue(row.reason, textValue(row.comment))}</p></div><Badge>{textValue(row.feedback_type, "weight")}</Badge><StatusBadge status={Boolean(row.enabled ?? true) ? "active" : "disabled"} /><Button variant="ghost" size="compact" loading={toggleRetrieval.isPending} onClick={() => toggleRetrieval.mutate(row)}>{Boolean(row.enabled ?? true) ? "停用" : "启用"}</Button></div>)}</div> : <EmptyState icon={ScanSearch} compact title="没有检索调权反馈" description="需要调权的坏案例会沿用后端反馈流程进入这里。" />}</section></TabsContent>
  </Tabs></div></div>;
}
