"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, FileCheck2, RefreshCw, ShieldCheck, ThumbsDown, X } from "lucide-react";
import { toast } from "sonner";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace-store";

type Desk = "knowledge" | "retrieval" | "claims";
type CandidateChoice = "skip" | "gold" | "negative";
type ClaimVerdict = "supported" | "unsupported" | "insufficient" | "not_factual";

interface UnitAnnotation {
  expectedStatus: "supported" | "no_evidence";
  retrievalQuery: string;
  recoveryQuery: string;
  choices: Record<string, CandidateChoice>;
}

interface ReviewInputState {
  key: string;
  note: string;
  verdict: ClaimVerdict | "";
}

interface UnitAnnotationState {
  key: string;
  values: Record<string, UnitAnnotation>;
}

const claimLabels: Record<ClaimVerdict, string> = {
  supported: "证据支持",
  unsupported: "证据反驳",
  insufficient: "证据不足",
  not_factual: "非事实声明",
};

function primaryId(row: JsonRecord, desk: Desk) {
  const keys = desk === "knowledge" ? ["knowledge_id", "id"] : desk === "retrieval" ? ["draft_id", "id"] : ["review_id", "id"];
  for (const key of keys) {
    const value = textValue(row[key], "");
    if (value) return value;
  }
  return "";
}

function rowTitle(row: JsonRecord, desk: Desk) {
  if (desk === "knowledge") return textValue(row.text, "待审核派生知识");
  if (desk === "retrieval") {
    const units = Array.isArray(row.units) ? row.units.filter(isRecord) : [];
    return textValue(row.query, textValue(units[0]?.label, "检索证据审核"));
  }
  return textValue(row.claim, textValue(row.claim_text, "声明核验"));
}

function directRecord(value: unknown, keys: string[] = []) {
  if (!isRecord(value)) return undefined;
  for (const key of keys) {
    if (isRecord(value[key])) return value[key];
  }
  return value;
}

function evidenceIdentity(row: JsonRecord) {
  const identity: JsonRecord = {
    chunk_id: textValue(row.chunk_id, ""),
    source: textValue(row.source, ""),
    source_sha256: textValue(row.source_sha256, ""),
  };
  const parentId = textValue(row.parent_chunk_id, "");
  if (parentId) identity.parent_chunk_id = parentId;
  return identity;
}

export default function ReviewsPage() {
  const queryClient = useQueryClient();
  const knowledgeBases = useKnowledgeBases();
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const activeKb = selectedKbId || knowledgeBases.data?.[0]?.kb_id || "";
  const [desk, setDesk] = useState<Desk>("knowledge");
  const [selectedId, setSelectedId] = useState("");
  const [reviewInput, setReviewInput] = useState<ReviewInputState>({ key: "", note: "", verdict: "" });
  const [unitAnnotationState, setUnitAnnotationState] = useState<UnitAnnotationState>({ key: "", values: {} });

  const knowledge = useQuery({ queryKey: ["reviews", "knowledge", activeKb], queryFn: () => controlApi.knowledge(activeKb), enabled: Boolean(activeKb) });
  const retrieval = useQuery({ queryKey: ["reviews", "retrieval", activeKb], queryFn: () => controlApi.retrievalEvalDrafts(activeKb), enabled: Boolean(activeKb) });
  const claims = useQuery({ queryKey: ["reviews", "claims"], queryFn: () => controlApi.claimReviews() });
  const claimSummary = useQuery({ queryKey: ["reviews", "claims", "summary"], queryFn: controlApi.claimReviewSummary, retry: false });
  const rowsByDesk = useMemo(() => ({
    knowledge: records(knowledge.data, ["items", "knowledge", "entries"]),
    retrieval: records(retrieval.data, ["items", "drafts"]),
    claims: records(claims.data, ["items", "reviews"]),
  }), [claims.data, knowledge.data, retrieval.data]);
  const visibleRows = rowsByDesk[desk];
  const activeId = selectedId || primaryId(visibleRows[0] ?? {}, desk);
  const listSelection = visibleRows.find((item) => primaryId(item, desk) === activeId) ?? visibleRows[0];
  const activeQuery = desk === "knowledge" ? knowledge : desk === "retrieval" ? retrieval : claims;

  const retrievalDetail = useQuery({
    queryKey: ["reviews", "retrieval-detail", activeId],
    queryFn: () => controlApi.retrievalEvalDraft(activeId),
    enabled: desk === "retrieval" && Boolean(activeId),
  });
  const candidateQuery = useQuery({
    queryKey: ["reviews", "retrieval-candidates", activeId],
    queryFn: () => controlApi.retrievalEvalCandidates(activeId),
    enabled: desk === "retrieval" && Boolean(activeId),
  });
  const claimDetail = useQuery({
    queryKey: ["reviews", "claim-detail", activeId],
    queryFn: () => controlApi.claimReview(activeId),
    enabled: desk === "claims" && Boolean(activeId),
  });
  const detailSelection = desk === "retrieval"
    ? directRecord(retrievalDetail.data, ["draft"])
    : desk === "claims"
      ? directRecord(claimDetail.data, ["review"])
      : listSelection;
  const selected = detailSelection ?? listSelection;
  const candidates = records(candidateQuery.data, ["items", "candidates"]);
  const units = useMemo(() => selected && Array.isArray(selected.units) ? selected.units.filter(isRecord) : [], [selected]);
  const selectionKey = `${desk}:${activeId}`;
  const reviewNote = reviewInput.key === selectionKey ? reviewInput.note : "";
  const claimVerdict = reviewInput.key === selectionKey ? reviewInput.verdict : "";
  const initialUnitAnnotations = useMemo(() => Object.fromEntries(units.map((unit, index) => {
    const unitId = textValue(unit.unit_id, `unit-${index + 1}`);
    const acceptable = Array.isArray(unit.acceptable_evidence) ? unit.acceptable_evidence.filter(isRecord) : [];
    const negatives = Array.isArray(unit.hard_negative_chunks) ? unit.hard_negative_chunks.filter(isRecord) : [];
    const choices: Record<string, CandidateChoice> = {};
    acceptable.forEach((item) => { choices[textValue(item.chunk_id, "")] = "gold"; });
    negatives.forEach((item) => { choices[textValue(item.chunk_id, "")] = "negative"; });
    return [unitId, {
      expectedStatus: textValue(unit.expected_status, "supported") === "no_evidence" ? "no_evidence" : "supported",
      retrievalQuery: textValue(unit.retrieval_query, ""),
      recoveryQuery: textValue(unit.recovery_query, ""),
      choices,
    } satisfies UnitAnnotation];
  })), [units]);
  const unitAnnotations = unitAnnotationState.key === selectionKey ? unitAnnotationState.values : initialUnitAnnotations;

  const setReviewNote = (note: string) => setReviewInput({ key: selectionKey, note, verdict: claimVerdict });
  const setClaimVerdict = (verdict: ClaimVerdict) => setReviewInput({ key: selectionKey, note: reviewNote, verdict });

  const updateUnit = (unitId: string, patch: Partial<UnitAnnotation>) => {
    setUnitAnnotationState((current) => {
      const values = current.key === selectionKey ? current.values : initialUnitAnnotations;
      const base = values[unitId];
      if (!base) return { key: selectionKey, values };
      return { key: selectionKey, values: { ...values, [unitId]: { ...base, ...patch } } };
    });
  };
  const chooseCandidate = (unitId: string, chunkId: string, choice: CandidateChoice) => {
    const current = unitAnnotations[unitId];
    if (!current) return;
    updateUnit(unitId, { choices: { ...current.choices, [chunkId]: choice } });
  };

  const mutation = useMutation({
    mutationFn: async ({ decision }: { decision: "approve" | "reject" | "label" }) => {
      if (!selected) throw new Error("请选择审核项");
      const id = primaryId(selected, desk) || activeId;
      const revision = numberValue(selected.revision, 1);
      if (desk === "knowledge") return controlApi.reviewKnowledge(id, decision, reviewNote.trim());
      if (desk === "claims") {
        if (!claimVerdict) throw new Error("请选择人工结论");
        return controlApi.labelClaimReview(id, { expected_verdict: claimVerdict, expected_revision: revision, review_note: reviewNote.trim() });
      }
      if (decision === "reject") {
        if (!reviewNote.trim()) throw new Error("驳回时必须填写原因");
        return controlApi.reviewRetrievalEval(id, { decision: "rejected", expected_revision: revision, reason: reviewNote.trim() });
      }
      const annotations = units.map((unit, index) => {
        const unitId = textValue(unit.unit_id, `unit-${index + 1}`);
        const value = unitAnnotations[unitId];
        if (!value?.retrievalQuery.trim() || !value.recoveryQuery.trim()) throw new Error(`${textValue(unit.label, unitId)}：检索词不能为空`);
        const selectedCandidates = candidates.filter((candidate) => value.choices[textValue(candidate.chunk_id, "")] === "gold");
        if (value.expectedStatus === "supported" && !selectedCandidates.length) throw new Error(`${textValue(unit.label, unitId)}：应有证据，请至少选择一段正确证据`);
        if (value.expectedStatus === "no_evidence" && selectedCandidates.length) throw new Error(`${textValue(unit.label, unitId)}：应无证据，不能选择正确证据`);
        const invalid = selectedCandidates.find((candidate) => !textValue(candidate.chunk_id, "") || !textValue(candidate.source, "") || !textValue(candidate.source_sha256, ""));
        if (invalid) throw new Error(`${textValue(unit.label, unitId)}：候选证据身份不完整`);
        return {
          unit_id: unitId,
          retrieval_query: value.retrievalQuery.trim(),
          recovery_query: value.recoveryQuery.trim(),
          expected_status: value.expectedStatus,
          acceptable_evidence: value.expectedStatus === "supported" ? selectedCandidates.map(evidenceIdentity) : [],
          hard_negative_chunks: candidates.filter((candidate) => value.choices[textValue(candidate.chunk_id, "")] === "negative").map(evidenceIdentity),
        };
      });
      return controlApi.reviewRetrievalEval(id, { decision: "approved", expected_revision: revision, annotations: { units: annotations }, reason: reviewNote.trim() });
    },
    onSuccess: async (_, variables) => {
      toast.success(variables.decision === "reject" ? "审核已拒绝" : desk === "claims" ? "人工结论已保存" : "审核已通过");
      setSelectedId("");
      await queryClient.invalidateQueries({ queryKey: ["reviews"] });
    },
    onError: (error) => toast.error(error.message),
  });

  const setDeskValue = (value: string) => {
    setDesk(value as Desk);
    setSelectedId("");
  };
  const detailPending = desk === "retrieval" ? retrievalDetail.isPending || candidateQuery.isPending : desk === "claims" ? claimDetail.isPending : false;
  const detailError = desk === "retrieval" ? retrievalDetail.error || candidateQuery.error : desk === "claims" ? claimDetail.error : null;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader eyebrow="Governance" title="审核" description="在证据旁完成派生知识、检索标注与声明核验，所有决定沿用后端修订和审计规则。" />
      <div className="flex min-h-12 flex-wrap items-center gap-3 border-b border-border bg-surface px-5 py-2">
        <Label htmlFor="review-kb" className="text-muted-foreground">知识库</Label>
        <Select value={activeKb} onValueChange={(value) => { setSelectedKbId(value); setSelectedId(""); }}>
          <SelectTrigger id="review-kb" className="w-60"><SelectValue placeholder="选择知识库" /></SelectTrigger>
          <SelectContent>{(knowledgeBases.data ?? []).map((kb) => <SelectItem key={kb.kb_id} value={kb.kb_id}>{kb.kb_id}</SelectItem>)}</SelectContent>
        </Select>
        <Tabs value={desk} onValueChange={setDeskValue} className="ml-2">
          <TabsList className="h-9 border-0">
            <TabsTrigger value="knowledge">派生知识 <Badge className="ml-1">{rowsByDesk.knowledge.length}</Badge></TabsTrigger>
            <TabsTrigger value="retrieval">检索证据 <Badge className="ml-1">{rowsByDesk.retrieval.length}</Badge></TabsTrigger>
            <TabsTrigger value="claims">声明核验 <Badge className="ml-1">{rowsByDesk.claims.length}</Badge></TabsTrigger>
          </TabsList>
        </Tabs>
        <Button variant="ghost" size="icon" className="ml-auto" onClick={() => void activeQuery.refetch()} aria-label="刷新审核队列"><RefreshCw className={cn("size-4", activeQuery.isFetching && "animate-spin")} /></Button>
      </div>
      {desk === "claims" && claimSummary.data ? <ClaimSummary value={directRecord(claimSummary.data) ?? {}} /> : null}
      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(320px,0.8fr)_minmax(520px,1.4fr)]">
        <section className="min-h-0 border-r border-border bg-surface lg:overflow-auto">
          <QueryState pending={activeQuery.isPending} error={activeQuery.error} onRetry={() => void activeQuery.refetch()} label="正在读取审核队列" />
          {!activeQuery.isPending && !visibleRows.length ? <EmptyState icon={FileCheck2} compact title="当前队列已清空" description="新的待审核内容会按服务端状态出现在这里。" /> : null}
          <div className="divide-y divide-border">
            {visibleRows.map((row) => {
              const id = primaryId(row, desk);
              return (
                <button key={id} onClick={() => setSelectedId(id)} className={cn("grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3 text-left hover:bg-surface-subtle", activeId === id && "bg-primary-subtle")}>
                  <span className="min-w-0"><span className="line-clamp-2 text-[13px] font-medium leading-5">{rowTitle(row, desk)}</span><span className="mt-1 block truncate font-mono text-[10px] text-muted-foreground">{id}</span></span>
                  <span className="flex items-center gap-2"><StatusBadge status={textValue(row.status, "pending")} /><ChevronRight className="size-3.5 text-muted-foreground" /></span>
                </button>
              );
            })}
          </div>
        </section>
        <section className="min-w-0 bg-surface">
          <QueryState pending={detailPending} error={detailError} label="正在读取受权限保护的证据详情" />
          {!detailPending && !detailError && selected ? (
            <div>
              <div className="border-b border-border px-5 py-4">
                <div className="flex items-center gap-2"><ShieldCheck className="size-4 text-primary" /><p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{desk === "knowledge" ? "Derived knowledge" : desk === "retrieval" ? "Retrieval evidence" : "Claim verification"}</p></div>
                <h3 className="mt-2 text-base font-semibold leading-6">{rowTitle(selected, desk)}</h3>
                {desk === "claims" ? <p className="mt-1 text-xs text-muted-foreground">模型判定：{claimLabels[textValue(selected.actual_verdict, "not_factual") as ClaimVerdict] ?? textValue(selected.actual_verdict)}</p> : null}
              </div>
              <div className="space-y-5 p-5">
                {desk === "retrieval" ? (
                  <RetrievalAnnotationEditor units={units} candidates={candidates} values={unitAnnotations} onUnitChange={updateUnit} onCandidateChange={chooseCandidate} />
                ) : (
                  <>
                    <div className="border-l-2 border-primary bg-primary-subtle px-4 py-3"><p className="text-[11px] font-medium text-primary">审核内容</p><p className="mt-1 whitespace-pre-wrap text-[13px] leading-6">{textValue(selected.text, textValue(selected.answer, textValue(selected.claim_text, textValue(selected.claim, textValue(selected.query)))))}</p></div>
                    <DetailFacts selected={selected} />
                    <EvidenceList value={selected.evidence} />
                  </>
                )}
                {desk === "claims" ? (
                  <div className="space-y-1.5"><Label>人工结论</Label><Select value={claimVerdict} onValueChange={(value) => setClaimVerdict(value as ClaimVerdict)}><SelectTrigger><SelectValue placeholder="独立判断后选择结论" /></SelectTrigger><SelectContent>{Object.entries(claimLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div>
                ) : null}
                <div className="space-y-1.5"><Label htmlFor="review-note">{desk === "retrieval" ? "审核说明（驳回时必填）" : "审核备注"}</Label><Textarea id="review-note" value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="记录判断依据，便于审计和后续复核。" /></div>
                <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
                  {desk !== "claims" ? <Button variant="secondary" onClick={() => mutation.mutate({ decision: "reject" })} loading={mutation.isPending}><X className="size-4" />拒绝</Button> : null}
                  <Button variant="primary" disabled={desk === "claims" && !claimVerdict} onClick={() => mutation.mutate({ decision: desk === "claims" ? "label" : "approve" })} loading={mutation.isPending}><Check className="size-4" />{desk === "claims" ? "保存人工结论" : "确认并通过"}</Button>
                </div>
              </div>
            </div>
          ) : !detailPending && !detailError ? <EmptyState icon={ThumbsDown} title="选择一个审核项" description="内容、证据和修订信息会在这里并排显示。" /> : null}
        </section>
      </div>
    </div>
  );
}

function ClaimSummary({ value }: { value: JsonRecord }) {
  const rate = typeof value.agreement_rate === "number" ? `${(value.agreement_rate * 100).toFixed(1)}%` : "—";
  return <div className="grid gap-px border-b border-border bg-border sm:grid-cols-4">{[["待审核", textValue(value.pending_count, "0")], ["已审核", textValue(value.reviewed_count, "0")], ["人机一致率", rate], ["证据不完整", textValue(value.evidence_incomplete_count, "0")]].map(([label, count]) => <div key={label} className="bg-surface px-5 py-2.5"><p className="text-[10px] text-muted-foreground">{label}</p><p className="mt-0.5 text-sm font-semibold tabular-nums">{count}</p></div>)}</div>;
}

function DetailFacts({ selected }: { selected: JsonRecord }) {
  return <dl className="grid gap-px overflow-hidden border border-border bg-border sm:grid-cols-2">{[["状态", textValue(selected.status, "pending")], ["修订", numberValue(selected.revision, 1)], ["来源", textValue(selected.source, textValue(selected.related_source))], ["创建时间", textValue(selected.created_at)]].map(([label, value]) => <div key={String(label)} className="bg-surface px-3 py-2.5"><dt className="text-[11px] text-muted-foreground">{label}</dt><dd className="mt-0.5 truncate text-xs">{String(value)}</dd></div>)}</dl>;
}

function EvidenceList({ value }: { value: unknown }) {
  const evidence = Array.isArray(value) ? value.filter(isRecord) : [];
  if (!evidence.length) return <div className="border-l-2 border-warning bg-warning-subtle px-3 py-2 text-xs text-warning">没有可展示的引用证据快照，请将证据缺失纳入人工判断。</div>;
  return <div><h4 className="mb-2 text-xs font-semibold">精确引用证据 · {evidence.length} 段</h4><div className="divide-y divide-border border border-border">{evidence.map((item, index) => <div key={textValue(item.chunk_id, String(index))} className="grid grid-cols-[32px_minmax(0,1fr)] gap-3 px-3 py-3"><span className="font-mono text-[10px] text-primary">#{String(index + 1).padStart(2, "0")}</span><div><p className="text-xs font-medium">{textValue(item.source, `证据 ${index + 1}`)}</p><p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{textValue(item.text, textValue(item.excerpt))}</p><p className="mt-1 font-mono text-[9px] text-muted-foreground">{textValue(item.chunk_id)}</p></div></div>)}</div></div>;
}

function RetrievalAnnotationEditor({
  units,
  candidates,
  values,
  onUnitChange,
  onCandidateChange,
}: {
  units: JsonRecord[];
  candidates: JsonRecord[];
  values: Record<string, UnitAnnotation>;
  onUnitChange: (unitId: string, patch: Partial<UnitAnnotation>) => void;
  onCandidateChange: (unitId: string, chunkId: string, choice: CandidateChoice) => void;
}) {
  if (!units.length) return <div className="border-l-2 border-error bg-error-subtle px-3 py-2 text-xs text-error">这份草稿没有可审核的原子需求，不能提交。</div>;
  return <div className="space-y-6">{units.map((unit, index) => {
    const unitId = textValue(unit.unit_id, `unit-${index + 1}`);
    const value = values[unitId];
    if (!value) return null;
    return <section key={unitId} className="border border-border"><div className="border-b border-border bg-surface-subtle px-4 py-3"><p className="font-mono text-[10px] uppercase text-primary">判题单 · {unitId}</p><h4 className="mt-1 text-sm font-semibold">{textValue(unit.label, `原子需求 ${index + 1}`)}</h4></div><div className="space-y-4 p-4"><div className="grid gap-3 sm:grid-cols-[180px_1fr_1fr]"><div className="space-y-1.5"><Label>证据预期</Label><Select value={value.expectedStatus} onValueChange={(status) => onUnitChange(unitId, { expectedStatus: status as UnitAnnotation["expectedStatus"] })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="supported">应有证据</SelectItem><SelectItem value="no_evidence">应无证据</SelectItem></SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor={`${unitId}-query`}>首轮检索词</Label><Input id={`${unitId}-query`} value={value.retrievalQuery} onChange={(event) => onUnitChange(unitId, { retrievalQuery: event.target.value })} /></div><div className="space-y-1.5"><Label htmlFor={`${unitId}-recovery`}>补救检索词</Label><Input id={`${unitId}-recovery`} value={value.recoveryQuery} onChange={(event) => onUnitChange(unitId, { recoveryQuery: event.target.value })} /></div></div><div><p className="mb-2 text-xs font-semibold">候选原文标注</p>{candidates.length ? <div className="divide-y divide-border border border-border">{candidates.map((candidate, candidateIndex) => { const chunkId = textValue(candidate.chunk_id, `candidate-${candidateIndex}`); return <div key={chunkId} className="grid grid-cols-[minmax(0,1fr)_130px] gap-3 px-3 py-3"><div><p className="text-xs font-medium">#{textValue(candidate.rank, String(candidateIndex + 1))} · {textValue(candidate.source, "未知来源")}</p><p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{textValue(candidate.text, "（空文本）")}</p><p className="mt-1 font-mono text-[9px] text-muted-foreground">{chunkId}</p></div><Select value={value.choices[chunkId] ?? "skip"} onValueChange={(choice) => onCandidateChange(unitId, chunkId, choice as CandidateChoice)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="skip">不标注</SelectItem><SelectItem value="gold">正确证据</SelectItem><SelectItem value="negative">误导项</SelectItem></SelectContent></Select></div>; })}</div> : <div className="border-l-2 border-warning bg-warning-subtle px-3 py-2 text-xs text-warning">本轮没有召回候选原文；只有“应无证据”的需求可以直接通过。</div>}</div></div></section>;
  })}</div>;
}
