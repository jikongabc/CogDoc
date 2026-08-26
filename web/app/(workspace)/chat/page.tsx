"use client";

import Link from "next/link";
import { ArrowRight, Library, MessageSquareText } from "lucide-react";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { formatDate } from "@/lib/utils";

export default function ChatLandingPage() {
  const knowledgeBases = useKnowledgeBases();

  return (
    <div className="min-h-full bg-background">
      <PageHeader
        eyebrow="Grounded assistant"
        title="证据对话"
        description="选择知识库开始对话。回答会保留引用、检索证据和反馈链路。"
      />
      <div className="p-4 md:p-6">
        <section className="overflow-hidden border border-border bg-surface">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div>
              <h3 className="text-sm font-semibold">可用知识库</h3>
              <p className="mt-0.5 text-[12px] text-muted-foreground">进入知识库后可继续历史会话或发起新问题。</p>
            </div>
            {knowledgeBases.data ? <span className="text-[11px] tabular-nums text-muted-foreground">{knowledgeBases.data.length} 个知识库</span> : null}
          </div>
          <QueryState pending={knowledgeBases.isPending} error={knowledgeBases.error} errorTitle="无法读取知识库" onRetry={() => void knowledgeBases.refetch()} />
          {knowledgeBases.data && !knowledgeBases.data.length ? (
            <EmptyState
              icon={MessageSquareText}
              title="没有可用于对话的知识库"
              description="先创建知识库并上传文档，索引完成后即可发起有依据的对话。"
              action={<Button asChild variant="primary"><Link href="/knowledge">前往知识管理</Link></Button>}
            />
          ) : null}
          {knowledgeBases.data?.length ? (
            <div className="divide-y divide-border">
              {knowledgeBases.data.map((kb) => (
                <Link
                  key={kb.kb_id}
                  href={`/knowledge/${encodeURIComponent(kb.kb_id)}`}
                  className="group grid min-h-14 grid-cols-[minmax(0,1fr)_110px_150px_24px] items-center gap-4 px-4 py-2.5 text-[13px] hover:bg-surface-subtle"
                >
                  <span className="flex min-w-0 items-center gap-3">
                    <span className="flex size-8 shrink-0 items-center justify-center rounded-[5px] bg-primary-subtle text-primary"><Library className="size-4" /></span>
                    <span className="min-w-0"><span className="block truncate font-medium">{kb.kb_id}</span><span className="block truncate text-[11px] text-muted-foreground">知识库对话空间</span></span>
                  </span>
                  <span className="tabular-nums text-muted-foreground">{kb.document_count} 个文档</span>
                  <span className="text-[11px] text-muted-foreground">创建于 {formatDate(kb.created_at)}</span>
                  <ArrowRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
                </Link>
              ))}
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
