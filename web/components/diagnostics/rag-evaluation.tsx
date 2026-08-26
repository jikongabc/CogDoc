"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, FileCheck2, LockKeyhole, RefreshCw, ShieldCheck, ThumbsDown, X } from "lucide-react";
import { toast } from "sonner";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { usePermission } from "@/features/auth/permissions";
import { ApiError } from "@/lib/api/client";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { cn } from "@/lib/utils";

type Desk = "retrieval" | "claims";
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
  const keys = desk === "retrieval" ? ["draft_id", "id"] : ["review_id", "id"];
  for (const key of keys) {
    const value = textValue(row[key], "");
    if (value) return value;
  }
  return "";
}

function rowTitle(row: JsonRecord, desk: Desk) {
  if (desk === "retrieval") {
    const units = Array.isArray(row.units) ? row.units.filter(isRecord) : [];
    return textValue(row.query, textValue(units[0]?.label, "检索评测"));
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

export function RagEvaluation({ kbId, onOpenRetrievalDiagnostics }: { kbId: string; onOpenRetrievalDiagnostics?: (query?: string) => void }) {
  const queryClient = useQueryClient();
  const canReview = usePermission("review");
  const [desk, setDesk] = useState<Desk>("retrieval");
  const [selectedId, setSelectedId] = useState("");
  const [reviewInput, setReviewInput] = useState<ReviewInputState>({ key: "", note: "", verdict: "" });
  const [unitAnnotationState, setUnitAnnotationState] = useState<UnitAnnotationState>({ key: "", values: {} });

  const retrieval = useQuery({ queryKey: ["reviews", "retrieval", kbId], queryFn: () => controlApi.retrievalEvalDrafts(kbId), enabled: canReview && desk === "retrieval" && Boolean(kbId), retry: false });
  const claims = useQuery({ queryKey: ["reviews", "claims", kbId], queryFn: () => controlApi.claimReviews(undefined, kbId), enabled: canReview && desk === "claims" && Boolean(kbId), retry: false });
  const claimSummary = useQuery({ queryKey: ["reviews", "claims", "summary", kbId], queryFn: () => controlApi.claimReviewSummary(kbId), enabled: canReview && desk === "claims" && Boolean(kbId), retry: false });
  const rowsByDesk = useMemo(() => ({
    retrieval: records(retrieval.data, ["items", "drafts"]),
    claims: records(claims.data, ["items", "reviews"]),
  }), [claims.data, retrieval.data]);
  const visibleRows = rowsByDesk[desk];
  const activeId = selectedId || primaryId(visibleRows[0] ?? {}, desk);
  const listSelection = visibleRows.find((item) => primaryId(item, desk) === activeId) ?? visibleRows[0];
  const listSelectionStale = desk === "retrieval" && listSelection?.is_stale === true;
  const activeQuery = desk === "retrieval" ? retrieval : claims;

  const retrievalDetail = useQuery({
    queryKey: ["reviews", "retrieval-detail", activeId],
    queryFn: () => controlApi.retrievalEvalDraft(activeId),
    enabled: canReview && desk === "retrieval" && Boolean(activeId),
    retry: false,
  });
  const candidateQuery = useQuery({
    queryKey: ["reviews", "retrieval-candidates", activeId],
    queryFn: () => controlApi.retrievalEvalCandidates(activeId),
    enabled: canReview && desk === "retrieval" && Boolean(activeId) && !listSelectionStale,
    retry: false,
  });
  const claimDetail = useQuery({
    queryKey: ["reviews", "claim-detail", activeId],
    queryFn: () => controlApi.claimReview(activeId),
    enabled: canReview && desk === "claims" && Boolean(activeId),
    retry: false,
  });
  const detailSelection = desk === "retrieval"
    ? directRecord(retrievalDetail.data, ["draft"])
    : directRecord(claimDetail.data, ["review"]);
  const selected = detailSelection ?? listSelection;
  const candidateConflict = candidateQuery.error instanceof ApiError && candidateQuery.error.status === 409;
  const selectedStale = desk === "retrieval" && (selected?.is_stale === true || candidateConflict);
  const staleReasons = selected && Array.isArray(selected.stale_reasons)
    ? selected.stale_reasons.filter((reason): reason is string => typeof reason === "string")
    : [];
  const candidates = records(candidateQuery.data, ["items", "candidates"]);
  const units = useMemo(() => selected && Array.isArray(selected.units) ? selected.units.filter(isRecord) : [], [selected]);
  const selectionKey = `${desk}:${activeId}`;
  const reviewNote = reviewInput.key === selectionKey ? reviewInput.note : "";
  const claimVerdict = reviewInput.key === selectionKey ? reviewInput.verdict : "";
  const initialUnitAnnotations = useMemo(() => Object.fromEntries(units.map((unit, index) => {
    const unitId = textValue(unit.unit_id, `unit-${index + 1}`);
    const retrievalQuery = textValue(unit.retrieval_query, "");
    const acceptable = Array.isArray(unit.acceptable_evidence) ? unit.acceptable_evidence.filter(isRecord) : [];
    const negatives = Array.isArray(unit.hard_negative_chunks) ? unit.hard_negative_chunks.filter(isRecord) : [];
    const choices: Record<string, CandidateChoice> = {};
    acceptable.forEach((item) => { choices[textValue(item.chunk_id, "")] = "gold"; });
    negatives.forEach((item) => { choices[textValue(item.chunk_id, "")] = "negative"; });
    return [unitId, {
      expectedStatus: textValue(unit.expected_status, "supported") === "no_evidence" ? "no_evidence" : "supported",
      retrievalQuery,
      // Diagnostic-created and older drafts may not have a fallback query.
      // Keep them reviewable while leaving the generated value editable.
      recoveryQuery: textValue(unit.recovery_query, retrievalQuery ? `${retrievalQuery} 补充证据` : ""),
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
      if (!selected) throw new Error("请选择评估项");
      const id = primaryId(selected, desk) || activeId;
      const revision = numberValue(selected.revision, 1);
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
        if (!value?.retrievalQuery.trim()) throw new Error(`${textValue(unit.label, unitId)}：首轮检索词不能为空`);
        if (!value.recoveryQuery.trim()) throw new Error(`${textValue(unit.label, unitId)}：补救检索词不能为空`);
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
      toast.success(variables.decision === "reject" ? "评测草稿已拒绝" : desk === "claims" ? "人工结论已保存" : "检索标注已保存");
      setSelectedId("");
      await queryClient.invalidateQueries({ queryKey: ["reviews"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const setDeskValue = (value: string) => {
    setDesk(value as Desk);
    setSelectedId("");
  };
  const detailPending = Boolean(activeId) && !selectedStale && (desk === "retrieval" ? retrievalDetail.isPending || candidateQuery.isPending : desk === "claims" ? claimDetail.isPending : false);
  const detailError = desk === "retrieval" ? retrievalDetail.error || (selectedStale ? null : candidateQuery.error) : desk === "claims" ? claimDetail.error : null;
  const activeAccessError = activeQuery.error instanceof ApiError && activeQuery.error.status === 403 ? activeQuery.error : null;

  if (!canReview) {
    return <EmptyState icon={LockKeyhole} title="当前角色没有 RAG 评测权限" description="RAG 评测需要 owner、admin、reviewer，或包含 review 权限的自定义角色。" />;
  }

  return (
    <div className="flex min-h-[520px] flex-col overflow-hidden border border-border bg-surface">
      <div className="flex min-h-14 flex-wrap items-center gap-3 border-b border-border px-4 py-2.5">
        <div className="mr-2 min-w-0"><h2 className="text-[13px] font-semibold">RAG 评测</h2><p className="truncate text-[11px] text-muted-foreground">当前知识库：{kbId} · 标注用于离线评测，不会直接改写检索结果</p></div>
        <Tabs value={desk} onValueChange={setDeskValue} className="sm:ml-auto">
          <TabsList className="h-9 border-0">
            <TabsTrigger value="retrieval">检索标注 <Badge className="ml-1">{retrieval.data === undefined ? "—" : rowsByDesk.retrieval.length}</Badge></TabsTrigger>
            <TabsTrigger value="claims">声明核验 <Badge className="ml-1">{claims.data === undefined ? "—" : rowsByDesk.claims.length}</Badge></TabsTrigger>
          </TabsList>
        </Tabs>
        <Button variant="ghost" size="icon" className="ml-auto" onClick={() => void activeQuery.refetch()} aria-label="刷新评估队列"><RefreshCw className={cn("size-4", activeQuery.isFetching && "animate-spin")} /></Button>
      </div>
      {activeAccessError ? <ReviewAccessState error={activeAccessError} /> : <>
        {desk === "claims" && claimSummary.data ? <ClaimSummary value={directRecord(claimSummary.data) ?? {}} /> : null}
        <div className="grid min-h-0 flex-1 xl:grid-cols-[minmax(320px,0.8fr)_minmax(520px,1.4fr)]">
        <section className="min-h-0 border-b border-border bg-surface xl:border-b-0 xl:border-r xl:overflow-auto">
          <QueryState pending={activeQuery.isPending} error={activeQuery.error} onRetry={() => void activeQuery.refetch()} label="正在读取评估队列" />
          {!activeQuery.isPending && !activeQuery.error && !visibleRows.length ? <EmptyState
            icon={FileCheck2}
            compact
            title={desk === "retrieval" ? "还没有检索评测草稿" : "还没有待核验声明"}
            description={desk === "retrieval" ? "先运行一次检索诊断，标记正确证据或误召回，再保存为评测草稿。" : "声明核验队列由问答后的抽样生成；当前知识库还没有待人工核验的声明。"}
            action={desk === "retrieval" && onOpenRetrievalDiagnostics ? <Button onClick={() => onOpenRetrievalDiagnostics()}>前往检索诊断</Button> : undefined}
          /> : null}
          <div className="divide-y divide-border">
            {visibleRows.map((row) => {
              const id = primaryId(row, desk);
              return (
                <button key={id} onClick={() => setSelectedId(id)} className={cn("grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3 text-left hover:bg-surface-subtle", activeId === id && "bg-primary-subtle")}>
                  <span className="min-w-0"><span className="line-clamp-2 text-[13px] font-medium leading-5">{rowTitle(row, desk)}</span><span className="mt-1 block truncate font-mono text-[10px] text-muted-foreground">{id}</span></span>
                  <span className="flex items-center gap-2"><StatusBadge status={row.is_stale === true ? "stale" : textValue(row.status, "pending")} /><ChevronRight className="size-3.5 text-muted-foreground" /></span>
                </button>
              );
            })}
          </div>
        </section>
        <section className="min-w-0 bg-surface">
          <QueryState pending={detailPending} error={detailError} label="正在读取受权限保护的证据详情" />
          {selectedStale && !detailPending && !detailError && selected ? <StaleDraftState query={textValue(selected.query, rowTitle(selected, "retrieval"))} reasons={staleReasons} onRefresh={onOpenRetrievalDiagnostics} /> : !detailPending && !detailError && selected ? (
            <div>
              <div className="border-b border-border px-5 py-4">
                <div className="flex items-center gap-2"><ShieldCheck className="size-4 text-primary" /><p className="text-[11px] font-semibold text-muted-foreground">{desk === "retrieval" ? "检索质量标注" : "声明核验"}</p></div>
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
                <div className="space-y-1.5"><Label htmlFor="review-note">{desk === "retrieval" ? "评估说明（驳回时必填）" : "核验备注"}</Label><Textarea id="review-note" value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="记录判断依据，便于审计和后续复核。" /></div>
                <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
                  {desk !== "claims" ? <Button variant="secondary" onClick={() => mutation.mutate({ decision: "reject" })} loading={mutation.isPending}><X className="size-4" />拒绝</Button> : null}
                  <Button variant="primary" disabled={desk === "claims" && !claimVerdict} onClick={() => mutation.mutate({ decision: desk === "claims" ? "label" : "approve" })} loading={mutation.isPending}><Check className="size-4" />{desk === "claims" ? "保存人工结论" : "确认并通过"}</Button>
                </div>
              </div>
            </div>
          ) : !detailPending && !detailError ? <EmptyState icon={ThumbsDown} title="选择一个评估项" description="内容、证据和标注信息会在这里并排显示。" /> : null}
        </section>
        </div>
      </>}
    </div>
  );
}

function ReviewAccessState({ error }: { error: ApiError }) {
  const disabled = error.message.includes("未启用");
  return <EmptyState icon={LockKeyhole} title={disabled ? "RAG 评测服务尚未加载账号权限" : "当前身份不能读取此评估队列"} description={disabled ? "账号登录模式不需要配置独立评估密钥。请重启后端以加载最新角色鉴权；只有旧版 API Key 部署才需要配置评估密钥。" : "请切换到 owner、admin、reviewer，或包含 review 权限的自定义角色后重试。"} className="flex-1" />;
}

function StaleDraftState({ query, reasons, onRefresh }: { query: string; reasons: string[]; onRefresh?: (query?: string) => void }) {
  const changedSources = reasons.filter((reason) => reason.startsWith("source_")).length;
  const description = changedSources
    ? `索引中的 ${changedSources} 个来源发生了变化。旧证据不能直接沿用，需要重新检索并确认。`
    : "知识库索引代际已经更新。为避免把旧候选误标为正确证据，请按当前索引重新运行诊断。";
  return <EmptyState
    icon={RefreshCw}
    title="这份评测草稿需要更新"
    description={description}
    action={onRefresh ? <Button variant="primary" onClick={() => onRefresh(query)}><RefreshCw className="size-4" />按当前索引重新诊断</Button> : undefined}
    className="min-h-[420px]"
  />;
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
