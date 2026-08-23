"use client";

import Link from "next/link";
import { ArrowRight, Files, Library, LockKeyhole } from "lucide-react";
import type { KnowledgeBase } from "@/lib/api/types";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { CreateKnowledgeBaseDialog } from "@/features/knowledge/create-kb-dialog";
import { DataGrid, type DataGridColumn } from "@/components/data-display/data-grid";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/utils";

export default function KnowledgePage() {
  const query = useKnowledgeBases();
  const columns: DataGridColumn<KnowledgeBase>[] = [
    { id: "name", header: "知识库", cell: (row) => <Link href={`/knowledge/${encodeURIComponent(row.kb_id)}`} className="flex w-fit items-center gap-2.5 rounded-[3px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"><span className="flex size-7 items-center justify-center rounded-[4px] bg-primary-subtle text-primary"><Library className="size-3.5" /></span><span><span className="block font-medium">{row.kb_id}</span><span className="block text-[11px] text-muted-foreground">{row.tenant_id}</span></span></Link> },
    { id: "documents", header: "文档", cell: (row) => <span className="flex items-center gap-1.5 text-muted-foreground"><Files className="size-3.5" />{row.document_count}</span> },
    { id: "created", header: "创建时间", cell: (row) => <span className="text-muted-foreground">{formatDate(row.created_at)}</span> },
    { id: "open", header: <span className="sr-only">打开</span>, className: "w-12", cell: (row) => <Link href={`/knowledge/${encodeURIComponent(row.kb_id)}`} aria-label={`打开知识库 ${row.kb_id}`} className="ml-auto flex size-8 items-center justify-center rounded-[5px] text-muted-foreground hover:bg-surface-subtle hover:text-foreground"><ArrowRight className="size-4" /></Link> },
  ];
  return (
    <div className="mx-auto w-full max-w-[1180px] p-4 md:p-6">
      <div className="mb-6 flex items-start justify-between gap-4"><div><h2 className="text-2xl font-semibold tracking-[-0.02em]">知识</h2><p className="mt-1 text-sm text-muted-foreground">管理可检索的文档、来源和访问边界。</p></div><CreateKnowledgeBaseDialog /></div>
      {query.isPending ? <div className="space-y-2 rounded-[5px] border border-border bg-surface p-3">{[0, 1, 2].map((item) => <Skeleton key={item} className="h-11 w-full" />)}</div> : null}
      {query.isError ? <div className="border-l-2 border-error bg-error-subtle px-4 py-3 text-sm text-error"><p className="font-medium">无法读取知识库</p><p className="mt-1 text-xs">{query.error.message}</p></div> : null}
      {query.data ? <DataGrid columns={columns} rows={query.data} rowKey={(row) => row.kb_id} empty={<div className="mx-auto max-w-sm"><span className="mx-auto mb-3 flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface-subtle text-muted-foreground"><LockKeyhole className="size-[18px]" /></span><p className="font-medium text-foreground">建立第一个知识库</p><p className="mt-1 text-xs text-muted-foreground">创建知识库后即可上传来源并开始带证据的对话。</p><div className="mt-4 flex justify-center"><CreateKnowledgeBaseDialog /></div></div>} /> : null}
    </div>
  );
}
