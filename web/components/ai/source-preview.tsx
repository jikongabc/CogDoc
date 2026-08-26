import { FileText, Fingerprint, MapPin } from "lucide-react";
import type { CitationLedgerEntry, Evidence } from "@/lib/api/types";
import { Badge } from "@/components/ui/badge";

export function sourceLocation(source: Pick<CitationLedgerEntry, "page" | "page_start" | "page_end" | "location">) {
  const location = source.location ?? {};
  const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : undefined;
  const string = (value: unknown) => typeof value === "string" && value.trim() ? value.trim() : undefined;
  const pageStart = source.page_start ?? number(location.page_start);
  const pageEnd = source.page_end ?? number(location.page_end);
  if (pageStart != null) return pageEnd != null && pageEnd !== pageStart ? `第 ${pageStart}–${pageEnd} 页` : `第 ${pageStart} 页`;
  if (source.page != null) return `第 ${source.page} 页`;
  const slide = number(location.slide);
  if (slide != null) return `第 ${slide} 张幻灯片`;
  const sheet = string(location.sheet);
  if (sheet) return `工作表 ${sheet}${string(location.cell_range) ? ` · ${string(location.cell_range)}` : ""}`;
  const lineStart = number(location.line_start);
  const lineEnd = number(location.line_end);
  if (lineStart != null) return lineEnd != null && lineEnd !== lineStart ? `第 ${lineStart}–${lineEnd} 行` : `第 ${lineStart} 行`;
  const image = number(location.image);
  if (image != null) return `第 ${image} 张图片`;
  if (Array.isArray(location.section_path)) {
    const path = location.section_path.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).join(" › ");
    if (path) return `章节 ${path}`;
  }
  const section = string(location.section);
  if (section) return `章节 ${section}`;
  const anchor = string(location.anchor);
  if (anchor) return `锚点 ${anchor}`;
  if (Array.isArray(location.bbox) && location.bbox.length === 4 && location.bbox.every((item) => typeof item === "number" && Number.isFinite(item))) {
    return `页面区域 ${location.bbox.map((item) => Number(item).toFixed(1)).join(", ")}`;
  }
  return "来源位置";
}

export function SourcePreview({ ledger, evidence }: { ledger: CitationLedgerEntry; evidence?: Evidence }) {
  const location = sourceLocation(ledger);
  const preview = evidence?.text_preview || "";
  const characters = Array.from(preview);
  const hasExactRange = ledger.span_start >= 0 && ledger.span_end > ledger.span_start && ledger.span_end <= characters.length;
  const before = hasExactRange ? characters.slice(0, ledger.span_start).join("") : "";
  const exact = hasExactRange ? characters.slice(ledger.span_start, ledger.span_end).join("") : "";
  const after = hasExactRange ? characters.slice(ledger.span_end).join("") : "";
  return (
    <article aria-labelledby={`source-${ledger.evidence_id}`}>
      <div className="flex items-start gap-3 border-b border-border px-4 py-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-[5px] bg-primary-subtle text-primary"><FileText className="size-4" /></span>
        <div className="min-w-0 flex-1"><h3 id={`source-${ledger.evidence_id}`} className="break-words text-sm font-semibold">{ledger.source || "派生知识"}</h3><p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"><MapPin className="size-3.5" />{location}</p></div>
        <Badge variant="success">已绑定</Badge>
      </div>
      <div className="space-y-4 p-4">
        {evidence?.section_title ? <div><p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">章节</p><p className="mt-1 text-[13px] font-medium">{evidence.section_path || evidence.section_title}</p></div> : null}
        <div><p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">证据摘录</p><blockquote className="mt-2 break-words border-l-2 border-primary bg-primary-subtle/60 px-3 py-2.5 text-[13px] leading-6 text-foreground">{preview ? hasExactRange ? <>{before}<mark className="bg-warning-subtle px-0.5 text-foreground">{exact}</mark>{after}</> : preview : "该历史记录未携带证据预览。可通过 Trace 查看原始证据。"}</blockquote>{preview && !hasExactRange ? <p className="mt-1.5 text-[11px] text-muted-foreground">当前预览未包含完整精确范围，以上内容仅作为相邻上下文。</p> : null}</div>
        <dl className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-2 border-t border-border pt-3 text-xs"><dt className="text-muted-foreground">证据 ID</dt><dd className="font-mono">{ledger.evidence_id}</dd><dt className="text-muted-foreground">片段</dt><dd className="truncate font-mono" title={ledger.chunk_id}>{ledger.chunk_id}</dd>{ledger.source_version_id ? <><dt className="text-muted-foreground">来源版本</dt><dd className="truncate font-mono" title={ledger.source_version_id}>{ledger.source_version_id}</dd></> : null}{evidence?.rerank_score != null ? <><dt className="text-muted-foreground">相关度</dt><dd>{evidence.rerank_score.toFixed(3)}</dd></> : null}</dl>
        <div className="flex items-start gap-2 rounded-[5px] bg-surface-subtle px-3 py-2 text-xs text-muted-foreground"><Fingerprint className="mt-0.5 size-3.5 shrink-0" /><span>此引用绑定到本次回答使用的固定来源位置。</span></div>
      </div>
    </article>
  );
}
