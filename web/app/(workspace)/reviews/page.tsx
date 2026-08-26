"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Spinner } from "@/components/ui/spinner";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { useWorkspaceStore } from "@/stores/workspace-store";

function RedirectStatus() {
  return <div className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-muted-foreground" role="status"><Spinner className="size-4" />正在打开 RAG 评测</div>;
}

function LegacyReviewsRedirectContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const legacyKbId = searchParams.get("kb")?.trim() ?? "";
  const knowledgeBases = useKnowledgeBases();
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const availableKbIds = knowledgeBases.data?.map((item) => item.kb_id) ?? [];
  const kbId = legacyKbId || (selectedKbId && availableKbIds.includes(selectedKbId)
    ? selectedKbId
    : availableKbIds[0]);

  useEffect(() => {
    if (knowledgeBases.isPending) return;
    router.replace(kbId
      ? `/knowledge/${encodeURIComponent(kbId)}/diagnostics?tab=rag`
      : "/knowledge");
  }, [kbId, knowledgeBases.isPending, router]);

  return <RedirectStatus />;
}

export default function LegacyReviewsRedirect() {
  return <Suspense fallback={<RedirectStatus />}><LegacyReviewsRedirectContent /></Suspense>;
}
