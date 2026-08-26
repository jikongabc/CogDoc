"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { ChevronRight, MessageSquareText, Upload } from "lucide-react";
import { toast } from "sonner";
import { useDocuments } from "@/features/knowledge/queries";
import { DocumentList } from "@/components/knowledge/document-list";
import { UploadZone } from "@/components/knowledge/upload-zone";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePermission } from "@/features/auth/permissions";
import { api } from "@/lib/api/client";
import type { Document } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { decodeRouteParam } from "@/lib/routing";
import { useSessionStore } from "@/stores/session-store";

export default function KnowledgeBasePage() {
  const params = useParams<{ kbId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const kbId = decodeRouteParam(params.kbId);
  const canQuery = usePermission("query");
  const canWrite = usePermission("write");
  const canDelete = usePermission("delete");
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const documents = useDocuments(kbId);
  const remove = useMutation({
    mutationFn: (document: Document) => api.deleteDocument(kbId, document.name),
    onSuccess: async () => {
      toast.success("文档已删除");
      await queryClient.invalidateQueries({ queryKey: queryKeys.documents(workspaceId, kbId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.knowledgeBases(workspaceId) });
    },
    onError: (error) => toast.error(error.message),
  });
  const startChat = () => router.push(`/knowledge/${encodeURIComponent(kbId)}/chat/${crypto.randomUUID()}`);
  return (
    <div className="mx-auto w-full max-w-[1180px] p-4 md:p-6">
      <nav className="mb-4 flex items-center gap-1 text-xs text-muted-foreground"><Link href="/knowledge" className="hover:text-foreground">知识</Link><ChevronRight className="size-3.5" /><span className="text-foreground">{kbId}</span></nav>
      <div className="mb-5 flex items-start justify-between gap-4"><div className="min-w-0"><h2 className="truncate text-2xl font-semibold tracking-[-0.02em]">{kbId}</h2><p className="mt-1 text-sm text-muted-foreground">{documents.data?.length ?? 0} 个已入库文档</p></div><Button variant="primary" onClick={startChat} disabled={!documents.data?.length || !canQuery} title={!canQuery ? "需要 Query 权限" : undefined}><MessageSquareText className="size-4" />开始对话</Button></div>
      <Tabs defaultValue="documents">
        <TabsList><TabsTrigger value="documents">文档</TabsTrigger><TabsTrigger value="upload" disabled={!canWrite} title={!canWrite ? "需要 Write 权限" : undefined}>上传</TabsTrigger></TabsList>
        <TabsContent value="documents" className="pt-4"><DocumentList documents={documents.data ?? []} loading={documents.isPending} onDelete={canDelete ? (document) => remove.mutate(document) : undefined} />{documents.isError ? <div className="mt-3 border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{documents.error.message}</div> : null}</TabsContent>
        <TabsContent value="upload" className="pt-4"><div className="mb-4"><h3 className="flex items-center gap-2 text-base font-semibold"><Upload className="size-4" />上传文档</h3><p className="mt-1 text-sm text-muted-foreground">文件会按现有 CogDoc 入库流程解析、切分并建立索引。</p></div><UploadZone kbId={kbId} /></TabsContent>
      </Tabs>
    </div>
  );
}
