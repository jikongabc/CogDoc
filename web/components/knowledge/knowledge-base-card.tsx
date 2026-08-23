import Link from "next/link";
import { ArrowRight, Files, Library } from "lucide-react";
import type { KnowledgeBase } from "@/lib/api/types";
import { formatDate } from "@/lib/utils";

export function KnowledgeBaseCard({ knowledgeBase }: { knowledgeBase: KnowledgeBase }) {
  return (
    <Link href={`/knowledge/${encodeURIComponent(knowledgeBase.kb_id)}`} className="group flex min-h-28 flex-col justify-between rounded-[5px] border border-border bg-surface p-4 shadow-[var(--shadow-edge)] hover:border-border-strong">
      <div className="flex items-start justify-between gap-4"><span className="flex size-8 items-center justify-center rounded-[5px] bg-primary-subtle text-primary"><Library className="size-4" /></span><ArrowRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" /></div>
      <div className="mt-4"><h3 className="truncate text-sm font-semibold">{knowledgeBase.kb_id}</h3><p className="mt-1 flex items-center gap-3 text-xs text-muted-foreground"><span className="flex items-center gap-1"><Files className="size-3.5" />{knowledgeBase.document_count} 个文档</span><span>{formatDate(knowledgeBase.created_at)}</span></p></div>
    </Link>
  );
}
