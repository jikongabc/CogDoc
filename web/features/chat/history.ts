import { api } from "@/lib/api/client";
import type {
  ChatResponse,
  Citation,
  CitationLedgerEntry,
  Evidence,
  HistoryMessage,
  TraceResponse,
} from "@/lib/api/types";
import type { MessageView } from "@/components/ai/message";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function optionalText(value: unknown) {
  return typeof value === "string" ? value : undefined;
}

function optionalNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function citation(value: unknown): Citation | null {
  const item = record(value);
  if (!item || typeof item.chunk_id !== "string") return null;
  return {
    chunk_id: item.chunk_id,
    source_type: text(item.source_type, "document"),
    knowledge_id: text(item.knowledge_id),
    source: text(item.source),
    source_id: optionalText(item.source_id),
    source_version_id: optionalText(item.source_version_id),
    media_type: optionalText(item.media_type),
    location: record(item.location) ?? record(item.source_location) ?? undefined,
    page: optionalNumber(item.page),
    page_start: optionalNumber(item.page_start),
    page_end: optionalNumber(item.page_end),
  };
}

function ledgerEntry(value: unknown): CitationLedgerEntry | null {
  const item = record(value);
  const source = citation(value);
  if (!item || !source || typeof item.evidence_id !== "string") return null;
  const spanStart = optionalNumber(item.span_start);
  const spanEnd = optionalNumber(item.span_end);
  if (spanStart === undefined || spanEnd === undefined || spanEnd <= spanStart) return null;
  const occurrences = Array.isArray(item.occurrences)
    ? item.occurrences.flatMap((value) => {
        const occurrence = record(value);
        const index = optionalNumber(occurrence?.index);
        const answerStart = optionalNumber(occurrence?.answer_start);
        const answerEnd = optionalNumber(occurrence?.answer_end);
        return index !== undefined && answerStart !== undefined && answerEnd !== undefined && answerEnd > answerStart
          ? [{ index, answer_start: answerStart, answer_end: answerEnd }]
          : [];
      })
    : [];
  return { ...source, evidence_id: item.evidence_id, span_start: spanStart, span_end: spanEnd, occurrences };
}

function evidenceItem(value: unknown): Evidence | null {
  const item = record(value);
  const source = citation(value);
  if (!item || !source || typeof item.text_preview !== "string") return null;
  return {
    ...source,
    parent_chunk_id: text(item.parent_chunk_id),
    section_title: text(item.section_title),
    section_path: text(item.section_path),
    section_level: optionalNumber(item.section_level),
    child_index_in_parent: optionalNumber(item.child_index_in_parent),
    chunk_index: optionalNumber(item.chunk_index),
    rerank_score: optionalNumber(item.rerank_score),
    rewrite_query: optionalText(item.rewrite_query),
    text_preview: item.text_preview,
    retrieval: record(item.retrieval) ?? {},
  };
}

const PUBLIC_CITATION_PATTERN = /\[(?:knowledge:[^\]\r\n]+|[^\]\r\n]+(?::P[0-9]+(?:-[0-9]+)?|@(?:slide|sheet|lines|image|section|anchor|region)-[^\]\r\n]+))\]/g;
const CANONICAL_EVIDENCE_TOKEN = /^\s*e[0-9]{3,}(?:\s*[,，;；]\s*e[0-9]{3,})*\s*[.,;:!?。！？；：]?\s*$/i;
const INVALID_EID_PAGE_TOKEN = /^\s*e[0-9]{3,}\s*:\s*p\s*[0-9]+\s*[.,;!?。！？；]?\s*$/i;
const MALFORMED_EVIDENCE_ID_FRAGMENT = "(?:e(?:[\\s:_-]*i[\\s:_-]*d)?[\\s:_-]+|e[\\s:_-]*i[\\s:_-]*d[\\s:_-]*|evidence[\\s:_-]*i[\\s:_-]*d[\\s:_-]*)[0-9]{3,}";
const MALFORMED_EVIDENCE_TOKEN = new RegExp(`^\\s*${MALFORMED_EVIDENCE_ID_FRAGMENT}\\s*[.,;:!?。！？；：]?\\s*$`, "i");

function sourceLabel(value: unknown) {
  return text(value).trim().replaceAll("%", "%25").replaceAll("[", "%5B").replaceAll("]", "%5D").replaceAll("\r", "%0D").replaceAll("\n", "%0A");
}

function locationLabel(location: Record<string, unknown>) {
  const pageStart = optionalNumber(location.page_start);
  const pageEnd = optionalNumber(location.page_end);
  if (pageStart !== undefined) return pageEnd !== undefined && pageEnd !== pageStart ? `P${pageStart}-${pageEnd}` : `P${pageStart}`;
  const slide = optionalNumber(location.slide);
  if (slide !== undefined) return `slide-${slide}`;
  const sheet = sourceLabel(location.sheet);
  if (sheet) return `sheet-${sheet}${sourceLabel(location.cell_range) ? `!${sourceLabel(location.cell_range)}` : ""}`;
  const lineStart = optionalNumber(location.line_start);
  const lineEnd = optionalNumber(location.line_end);
  if (lineStart !== undefined) return `lines-${lineStart}${lineEnd !== undefined && lineEnd !== lineStart ? `-${lineEnd}` : ""}`;
  const image = optionalNumber(location.image);
  if (image !== undefined) return `image-${image}`;
  if (Array.isArray(location.section_path)) {
    const section = sourceLabel(location.section_path.join("/"));
    if (section) return `section-${section}`;
  }
  const anchor = sourceLabel(location.anchor);
  if (anchor) return `anchor-${anchor}`;
  return Array.isArray(location.bbox) && location.bbox.length === 4 ? "region-bbox" : "";
}

function displayCitation(entry: CitationLedgerEntry) {
  if (entry.source_type === "derived_knowledge") return entry.knowledge_id ? `[knowledge:${entry.knowledge_id}]` : "";
  if (entry.source_type !== "document") return "";
  const source = sourceLabel(entry.source);
  if (!source) return "";
  const locator = locationLabel(record(entry.location) ?? {});
  if (locator.startsWith("P")) return `[${source}:${locator}]`;
  if (locator) return `[${source}@${locator}]`;
  return entry.page !== undefined && entry.page !== null ? `[${source}:P${entry.page}]` : "";
}

function canonicalEvidenceId(value: string) {
  if (!/^E\d{3,}$/.test(value)) return false;
  const number = Number(value.slice(1));
  return Number.isSafeInteger(number) && number > 0 && `E${String(number).padStart(3, "0")}` === value;
}

function codePointSlice(value: string, start: number, end?: number) {
  return Array.from(value).slice(start, end).join("");
}

function publicOccurrences(answer: string) {
  return [...answer.matchAll(PUBLIC_CITATION_PATTERN)].map((match) => {
    const start = Array.from(answer.slice(0, match.index)).length;
    return { start, end: start + Array.from(match[0]).length, display: match[0] };
  });
}

function containsInternalEvidenceReference(answer: string) {
  const masked = answer.replace(PUBLIC_CITATION_PATTERN, (value) => " ".repeat(value.length));
  const normalized = masked.normalize("NFKC");
  let cursor = 0;
  while (cursor < normalized.length) {
    const opening = normalized.indexOf("[", cursor);
    if (opening < 0) return false;
    const closing = normalized.indexOf("]", opening + 1);
    const lineEnd = normalized.indexOf("\n", opening + 1);
    const hasClosing = closing >= 0 && (lineEnd < 0 || closing < lineEnd);
    const end = hasClosing ? closing : lineEnd < 0 ? normalized.length : lineEnd;
    cursor = hasClosing ? closing + 1 : Math.max(end, opening + 1);
    const token = normalized.slice(opening + 1, end).split("[").at(-1)?.trim() ?? "";
    if (CANONICAL_EVIDENCE_TOKEN.test(token) || INVALID_EID_PAGE_TOKEN.test(token) || MALFORMED_EVIDENCE_TOKEN.test(token)) return true;
  }
  return false;
}

function meaningful(value: unknown) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value as object).length > 0;
  return true;
}

function sameValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) return left.length === right.length && left.every((item, index) => sameValue(item, right[index]));
  const leftRecord = record(left);
  const rightRecord = record(right);
  if (!leftRecord || !rightRecord) return false;
  const leftKeys = Object.keys(leftRecord).sort();
  const rightKeys = Object.keys(rightRecord).sort();
  return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && sameValue(leftRecord[key], rightRecord[key]));
}

function evidenceMatches(entry: CitationLedgerEntry, evidence: Record<string, unknown>) {
  if (evidence._metadata_conflict) return false;
  if (entry.chunk_id.trim() !== text(evidence.chunk_id).trim() || entry.source_type !== text(evidence.source_type, "document")) return false;
  const retrieval = record(evidence.retrieval) ?? {};
  const expectedEvidenceIds = [evidence.evidence_id, retrieval.evidence_id].filter(meaningful).map(String);
  if (expectedEvidenceIds.length && (new Set(expectedEvidenceIds).size !== 1 || expectedEvidenceIds[0] !== entry.evidence_id)) return false;
  for (const key of ["source", "source_id", "source_version_id", "media_type", "location", "knowledge_id", "page", "page_start", "page_end"] as const) {
    if (meaningful(evidence[key]) && !sameValue(entry[key], evidence[key])) return false;
  }
  const expectedStart = meaningful(retrieval.evidence_text_start) ? retrieval.evidence_text_start : evidence.span_start;
  const expectedEnd = meaningful(retrieval.evidence_text_end) ? retrieval.evidence_text_end : evidence.span_end;
  if (meaningful(expectedStart) && expectedStart !== entry.span_start) return false;
  if (meaningful(expectedEnd) && expectedEnd !== entry.span_end) return false;
  return true;
}

function validPublicLedger(answer: string, ledger: CitationLedgerEntry[], evidence: Record<string, unknown>[], structurallyComplete: boolean) {
  if (!structurallyComplete || containsInternalEvidenceReference(answer)) return false;
  const visible = publicOccurrences(answer);
  if (!ledger.length) return visible.length === 0;
  if (!evidence.length) return false;
  const evidenceIds = new Set<string>();
  const identities = new Set<string>();
  const declared: Array<{ start: number; end: number; index: number; display: string }> = [];
  for (const entry of ledger) {
    const identity = `${entry.chunk_id}\u0000${entry.span_start}\u0000${entry.span_end}`;
    const pageValues = [entry.page, entry.page_start, entry.page_end].filter((value) => value !== null && value !== undefined);
    if (!entry.chunk_id.trim() || pageValues.some((value) => !Number.isInteger(value) || Number(value) < 0) || (entry.page_start != null && entry.page_end != null && entry.page_end < entry.page_start) || (meaningful(entry.location) && !entry.source_version_id?.trim()) || !Number.isInteger(entry.span_start) || !Number.isInteger(entry.span_end) || entry.span_start < 0 || entry.span_end <= entry.span_start || !canonicalEvidenceId(entry.evidence_id) || evidenceIds.has(entry.evidence_id) || identities.has(identity)) return false;
    evidenceIds.add(entry.evidence_id);
    identities.add(identity);
    const display = displayCitation(entry);
    if (!display || !entry.occurrences.length || !evidence.some((item) => evidenceMatches(entry, item))) return false;
    for (const occurrence of entry.occurrences) {
      if (!Number.isInteger(occurrence.index) || !Number.isInteger(occurrence.answer_start) || !Number.isInteger(occurrence.answer_end) || occurrence.answer_start < 0 || occurrence.answer_end <= occurrence.answer_start || codePointSlice(answer, occurrence.answer_start, occurrence.answer_end) !== display) return false;
      declared.push({ start: occurrence.answer_start, end: occurrence.answer_end, index: occurrence.index, display });
    }
  }
  declared.sort((left, right) => left.start - right.start || left.end - right.end || left.index - right.index);
  if (declared.some((item, index) => item.index !== index || (index > 0 && item.start < (declared[index - 1]?.end ?? 0)))) return false;
  return declared.length === visible.length && declared.every((item, index) => item.start === visible[index]?.start && item.end === visible[index]?.end && item.display === visible[index]?.display);
}

function taskType(value: unknown): ChatResponse["task_type"] {
  return value === "qa" || value === "summary" || value === "compare" ? value : "unknown";
}

function responseFromFields(
  fields: Record<string, unknown>,
  traceId: string,
  kbId: string,
  sessionId: string,
  fallbackAnswer: string,
  requestId = "history",
): ChatResponse {
  const critique = text(fields.critique);
  const rawLedger = Array.isArray(fields.citation_ledger) ? fields.citation_ledger : [];
  const ledger = rawLedger.flatMap((item) => ledgerEntry(item) ?? []);
  const rawEvidence = Array.isArray(fields.evidence) ? fields.evidence : [];
  const evidence = rawEvidence.flatMap((item) => evidenceItem(item) ?? []);
  const rawEvidenceLedger = Array.isArray(fields.evidence_ledger) ? fields.evidence_ledger : [];
  const validationEvidence = (rawEvidenceLedger.length ? rawEvidenceLedger : rawEvidence).flatMap((item): Record<string, unknown>[] => {
    const row = record(item);
    return row ? [row] : [];
  });
  const answer = text(fields.answer, fallbackAnswer);
  const ledgerIsValid = validPublicLedger(answer, ledger, validationEvidence, rawLedger.length === ledger.length);
  const resultIsValid = typeof fields.is_valid === "boolean" ? fields.is_valid : !critique;
  return {
    schema_version: "v1",
    request_id: requestId,
    trace_id: traceId,
    doc_id: kbId,
    session_id: sessionId,
    task_type: taskType(fields.task_type),
    answer,
    citations: Array.isArray(fields.citations)
      ? fields.citations.flatMap((item) => citation(item) ?? [])
      : Array.isArray(fields.sources)
        ? fields.sources.flatMap((item) => citation(item) ?? [])
        : [],
    citation_ledger: ledgerIsValid ? ledger : [],
    evidence,
    critique: critique || (!ledgerIsValid ? "历史回答未通过引用完整性校验。" : ""),
    is_valid: resultIsValid && ledgerIsValid,
    claim_audit: record(fields.claim_audit),
    claim_verification: record(fields.claim_verification_rollout),
  };
}

export function historyMessage(
  message: HistoryMessage,
  index: number,
  kbId: string,
  sessionId: string,
): MessageView {
  const role = message.role === "assistant" ? "assistant" : "user";
  const traceId = optionalText(message.trace_id);
  const fields = message as Record<string, unknown>;
  const response = role === "assistant" && traceId
    ? responseFromFields(fields, traceId, kbId, sessionId, message.content)
    : undefined;
  const needsEvidence = Boolean(
    response && !Array.isArray(message.citation_ledger),
  );
  return {
    id: `history-${index}`,
    role,
    content: message.content || "",
    query: optionalText(message.query),
    response,
    evidenceStatus: needsEvidence ? "loading" : undefined,
  };
}

export async function hydrateHistoryEvidence(
  message: MessageView,
  kbId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<MessageView> {
  if (message.role !== "assistant" || message.evidenceStatus !== "loading" || !message.response?.trace_id) {
    return message;
  }
  try {
    const trace: TraceResponse = await api.trace(message.response.trace_id, signal);
    return {
      ...message,
      content: text(trace.output.answer, message.content),
      response: responseFromFields(
        { ...trace.output, task_type: trace.task_type },
        trace.trace_id,
        kbId,
        sessionId,
        message.content,
        trace.request_id,
      ),
      evidenceStatus: undefined,
    };
  } catch {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return { ...message, evidenceStatus: "unavailable" };
  }
}
