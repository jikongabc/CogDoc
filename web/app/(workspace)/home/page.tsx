"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { BookOpenCheck, LoaderCircle } from "lucide-react";
import { CreateKnowledgeBaseDialog } from "@/features/knowledge/create-kb-dialog";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { useSessionStore } from "@/stores/session-store";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";

export default function WorkspaceHomePage() {
  const router = useRouter();
  const knowledgeBases = useKnowledgeBases();
  const workspace = useSessionStore((state) => state.workspace);
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const selectedKb = knowledgeBases.data?.find((kb) => kb.kb_id === selectedKbId)?.kb_id || knowledgeBases.data?.[0]?.kb_id || "";
  const sessions = useQuery({
    queryKey: queryKeys.sessions(workspaceId, selectedKb),
    queryFn: () => api.sessions(selectedKb),
    enabled: Boolean(selectedKb),
  });
  const sessionId = useRef<string | null>(null);

  useEffect(() => {
    if (!selectedKb || sessions.isPending) return;
    sessionId.current ||= sessions.data?.sessions?.[0]?.session_id || crypto.randomUUID();
    setSelectedKbId(selectedKb);
    router.replace(`/knowledge/${encodeURIComponent(selectedKb)}/chat/${sessionId.current}`);
  }, [router, selectedKb, sessions.data, sessions.isPending, setSelectedKbId]);

  if (knowledgeBases.isError) {
    return <div className="mx-auto mt-16 max-w-lg border-l-2 border-error bg-error-subtle px-4 py-3 text-sm text-error"><p className="font-medium">无法打开知识工作台</p><p className="mt-1 text-xs">{knowledgeBases.error.message}</p></div>;
  }

  if (knowledgeBases.data && !knowledgeBases.data.length) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <div className="max-w-md text-center">
          <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{workspace?.name || "本地工作区"}</p>
          <span className="mx-auto flex size-10 items-center justify-center rounded-[6px] border border-border bg-surface text-primary"><BookOpenCheck className="size-5" /></span>
          <h2 className="mt-4 text-xl font-semibold tracking-[-0.02em]">创建第一个知识库</h2>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">创建知识库、上传文档，然后就能沿用原来的 CogDoc 工作流进行对话、研究和证据审核。</p>
          <div className="mt-5 flex justify-center"><CreateKnowledgeBaseDialog /></div>
        </div>
      </div>
    );
  }

  return <div className="flex min-h-full items-center justify-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin text-primary" />正在恢复知识库与对话</div>;
}
