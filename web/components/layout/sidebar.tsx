"use client";

import Link from "next/link";
import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpenCheck,
  Check,
  ChevronLeft,
  ChevronRight,
  FileText,
  FolderCog,
  Library,
  ListChecks,
  LoaderCircle,
  LogOut,
  MessageSquarePlus,
  MessagesSquare,
  MoreHorizontal,
  Settings,
  Trash2,
  Upload,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { knowledgeBaseFromPath, sessionFromPath, workbenchHref, workbenchViewFromPath } from "@/lib/workbench";
import { cn } from "@/lib/utils";
import { useAnyPermission, usePermission } from "@/features/auth/permissions";
import { useDocuments, useKnowledgeBases } from "@/features/knowledge/queries";
import { CreateKnowledgeBaseDialog } from "@/features/knowledge/create-kb-dialog";
import { UploadZone } from "@/components/knowledge/upload-zone";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { WorkspaceSwitcher } from "./workspace-switcher";
import { useSessionStore } from "@/stores/session-store";
import { useWorkspaceStore } from "@/stores/workspace-store";

function RailSection({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="border-t border-border px-2 py-3">
      <div className="mb-2 flex min-h-6 items-center justify-between gap-2 px-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function CollapsedLink({ href, label, icon: Icon }: { href: string; label: string; icon: typeof Library }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild variant="ghost" size="icon"><Link href={href} aria-label={label}><Icon className="size-4" /></Link></Button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

export function Sidebar({ className, forceExpanded = false }: { className?: string; forceExpanded?: boolean }) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const storedCollapsed = useSessionStore((state) => state.sidebarCollapsed);
  const collapsed = forceExpanded ? false : storedCollapsed;
  const setCollapsed = useSessionStore((state) => state.setSidebarCollapsed);
  const user = useSessionStore((state) => state.user);
  const clearSession = useSessionStore((state) => state.clearSession);
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const rememberConversation = useWorkspaceStore((state) => state.rememberConversation);
  const clearKnowledgeContext = useWorkspaceStore((state) => state.clearKnowledgeContext);
  const localMode = useWorkspaceStore((state) => state.localModelMode);
  const setLocalMode = useWorkspaceStore((state) => state.setLocalModelMode);
  const canWrite = usePermission("write");
  const canDelete = usePermission("delete");
  const canManage = useAnyPermission(["manage_access", "manage_tenant"]);
  const knowledgeBases = useKnowledgeBases();
  const routeKbId = knowledgeBaseFromPath(pathname);
  const selectedKbIsAvailable = Boolean(selectedKbId && knowledgeBases.data?.some((kb) => kb.kb_id === selectedKbId));
  const kbId = routeKbId || (selectedKbIsAvailable ? selectedKbId : null) || knowledgeBases.data?.[0]?.kb_id || "";
  const activeSessionId = sessionFromPath(pathname);
  const sessions = useQuery({
    queryKey: queryKeys.sessions(workspaceId, kbId),
    queryFn: () => api.sessions(kbId),
    enabled: Boolean(kbId),
  });
  const documents = useDocuments(kbId);

  useEffect(() => {
    const queryKb = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("kb") : null;
    const queryKbIsAvailable = Boolean(queryKb && knowledgeBases.data?.some((kb) => kb.kb_id === queryKb));
    const candidate = routeKbId || (queryKbIsAvailable ? queryKb : null) || (selectedKbIsAvailable ? selectedKbId : null) || knowledgeBases.data?.[0]?.kb_id;
    if (candidate && candidate !== selectedKbId) setSelectedKbId(candidate);
  }, [knowledgeBases.data, routeKbId, selectedKbId, selectedKbIsAvailable, setSelectedKbId]);

  useEffect(() => {
    if (kbId && activeSessionId) rememberConversation(kbId, activeSessionId);
  }, [activeSessionId, kbId, rememberConversation]);

  const newConversation = (targetKb = kbId) => {
    if (!targetKb) return;
    setSelectedKbId(targetKb);
    router.push(`/knowledge/${encodeURIComponent(targetKb)}/chat/${crypto.randomUUID()}`);
  };

  const selectKnowledgeBase = (targetKb: string) => {
    setSelectedKbId(targetKb);
    const view = workbenchViewFromPath(pathname);
    if (!view || view === "conversation") {
      newConversation(targetKb);
      return;
    }
    router.push(`${workbenchHref(view, targetKb)}?kb=${encodeURIComponent(targetKb)}`);
  };

  const removeSession = useMutation({
    mutationFn: (sessionId: string) => api.deleteSession(kbId, sessionId),
    onSuccess: async (_, deletedId) => {
      toast.success("对话已删除");
      await queryClient.invalidateQueries({ queryKey: queryKeys.sessions(workspaceId, kbId) });
      if (deletedId === activeSessionId) newConversation();
    },
    onError: (error) => toast.error(error.message),
  });

  const removeDocument = useMutation({
    mutationFn: (name: string) => api.deleteDocument(kbId, name),
    onSuccess: async () => {
      toast.success("文档已删除");
      await queryClient.invalidateQueries({ queryKey: queryKeys.documents(workspaceId, kbId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.knowledgeBases(workspaceId) });
    },
    onError: (error) => toast.error(error.message),
  });

  const logout = async () => {
    try { await api.logout(); } catch { /* Browser cleanup remains authoritative. */ }
    clearSession();
    clearKnowledgeContext();
    setLocalMode(false);
    queryClient.clear();
    router.replace("/login");
    toast.success("已退出登录");
  };

  if (collapsed) {
    return (
      <aside className={cn("flex h-dvh w-14 shrink-0 flex-col items-center border-r border-border bg-surface py-2", className)}>
        <Link href="/home" className="mb-2 flex size-8 items-center justify-center rounded-[5px] bg-foreground text-white" aria-label="CogDoc 工作台"><BookOpenCheck className="size-[17px]" /></Link>
        <WorkspaceSwitcher collapsed />
        <div className="my-2 h-px w-8 bg-border" />
        {kbId ? <CollapsedLink href={`/knowledge/${encodeURIComponent(kbId)}`} label={kbId} icon={Library} /> : null}
        <CollapsedLink href="/knowledge" label="全部知识库" icon={FolderCog} />
        <CollapsedLink href="/tasks" label="后台任务" icon={ListChecks} />
        {canManage ? <CollapsedLink href="/admin" label="管理" icon={Settings} /> : null}
        <div className="flex-1" />
        {!forceExpanded ? <Button variant="ghost" size="icon" onClick={() => setCollapsed(false)} aria-label="展开侧栏"><ChevronRight className="size-4" /></Button> : null}
      </aside>
    );
  }

  const serverSessions = sessions.data?.sessions ?? [];
  const visibleSessions = activeSessionId && !serverSessions.some((item) => item.session_id === activeSessionId)
    ? [{ session_id: activeSessionId, title: "新对话", message_count: 0, updated_at: "" }, ...serverSessions]
    : serverSessions;

  return (
    <aside className={cn("flex h-dvh w-72 shrink-0 flex-col border-r border-border bg-surface", className)}>
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-3">
        <Link href="/home" className="flex items-center gap-2" aria-label="CogDoc 工作台">
          <span className="flex size-7 items-center justify-center rounded-[5px] bg-foreground text-white"><BookOpenCheck className="size-4" /></span>
          <span><span className="block text-sm font-semibold tracking-[-0.01em]">CogDoc</span><span className="block text-[10px] leading-3 text-muted-foreground">知识工作台</span></span>
        </Link>
        {!forceExpanded ? <Button variant="ghost" size="icon" onClick={() => setCollapsed(true)} aria-label="收起侧栏"><ChevronLeft className="size-4" /></Button> : null}
      </div>

      <div className="shrink-0 p-2"><WorkspaceSwitcher /></div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <RailSection title="知识库">
          {knowledgeBases.isPending ? <div className="flex h-9 items-center gap-2 px-2 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" />正在读取知识库</div> : knowledgeBases.isError ? <div role="alert" className="border-l-2 border-error bg-error-subtle px-2 py-2 text-xs text-error">无法读取知识库</div> : knowledgeBases.data?.length ? (
            <select
              aria-label="选择知识库"
              value={kbId}
              onChange={(event) => selectKnowledgeBase(event.target.value)}
              className="h-9 w-full rounded-[5px] border border-border bg-background px-2 text-[13px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              {knowledgeBases.data.map((kb) => <option key={kb.kb_id} value={kb.kb_id}>{kb.kb_id} · {kb.document_count} 文档</option>)}
            </select>
          ) : <p className="px-2 py-2 text-xs leading-5 text-muted-foreground">还没有知识库，先创建一个。</p>}
          <div className="mt-2 flex items-center justify-between gap-2">
            <CreateKnowledgeBaseDialog />
            {kbId ? <Button asChild variant="ghost" size="compact"><Link href={`/knowledge/${encodeURIComponent(kbId)}/access`}><FolderCog className="size-3.5" />访问权限</Link></Button> : null}
          </div>
        </RailSection>

        {kbId ? (
          <>
            <RailSection title="对话" action={<Button variant="ghost" size="compact" onClick={() => newConversation()}><MessageSquarePlus className="size-3.5" />新对话</Button>}>
              {sessions.isPending ? <div className="flex h-12 items-center justify-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" />正在读取</div> : sessions.isError ? <button className="w-full border-l-2 border-error bg-error-subtle px-2 py-2 text-left text-xs text-error" onClick={() => void sessions.refetch()}><span className="block font-medium">无法读取对话</span><span className="mt-0.5 block opacity-80">点击重试</span></button> : visibleSessions.length ? (
                <nav className="space-y-0.5" aria-label="对话记录">
                  {visibleSessions.slice(0, 30).map((session) => (
                    <div key={session.session_id} className={cn("group flex items-center rounded-[5px]", session.session_id === activeSessionId && "bg-primary-subtle")}>
                      <Link href={`/knowledge/${encodeURIComponent(kbId)}/chat/${encodeURIComponent(session.session_id)}`} className={cn("min-w-0 flex-1 px-2 py-1.5 text-[13px] text-muted-foreground hover:text-foreground", session.session_id === activeSessionId && "text-primary")}>
                        <span className="block truncate">{session.title || "未命名对话"}</span>
                        <span className="block text-[10px] opacity-70">{session.message_count} 条消息</span>
                      </Link>
                      {canDelete ? <Button variant="ghost" size="icon" className="mr-0.5 size-7 opacity-0 group-hover:opacity-100 focus-visible:opacity-100" onClick={() => removeSession.mutate(session.session_id)} aria-label={`删除对话 ${session.title || "未命名对话"}`}><Trash2 className="size-3.5" /></Button> : null}
                    </div>
                  ))}
                </nav>
              ) : <div className="px-2 py-5 text-center text-xs text-muted-foreground"><MessagesSquare className="mx-auto mb-2 size-4" />还没有历史对话</div>}
            </RailSection>

            <RailSection title="文档" action={canWrite ? (
              <Dialog>
                <DialogTrigger asChild><Button variant="ghost" size="compact"><Upload className="size-3.5" />上传</Button></DialogTrigger>
                <DialogContent className="max-w-2xl">
                  <DialogHeader><DialogTitle>上传文档到 {kbId}</DialogTitle><DialogDescription>沿用 CogDoc 现有解析、切分、OCR 和索引流程。</DialogDescription></DialogHeader>
                  <UploadZone kbId={kbId} />
                </DialogContent>
              </Dialog>
            ) : undefined}>
              {documents.isPending ? <div className="flex h-12 items-center justify-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" />正在读取</div> : documents.isError ? <button className="w-full border-l-2 border-error bg-error-subtle px-2 py-2 text-left text-xs text-error" onClick={() => void documents.refetch()}>读取失败，点击重试</button> : documents.data?.length ? (
                <div className="space-y-0.5">
                  {documents.data.map((document) => (
                    <div key={document.name} className="group flex min-h-8 items-center rounded-[5px] px-2 hover:bg-surface-subtle">
                      <FileText className="mr-2 size-3.5 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate text-xs" title={document.name}>{document.name}</span>
                      {canDelete ? <Button variant="ghost" size="icon" className="size-7 opacity-0 group-hover:opacity-100 focus-visible:opacity-100" onClick={() => removeDocument.mutate(document.name)} aria-label={`删除文档 ${document.name}`}><Trash2 className="size-3.5" /></Button> : null}
                    </div>
                  ))}
                </div>
              ) : <p className="px-2 py-3 text-xs text-muted-foreground">暂无文档。上传后即可开始检索。</p>}
              <label className="mt-2 flex min-h-8 cursor-pointer items-center gap-2 rounded-[5px] px-2 text-xs text-muted-foreground hover:bg-surface-subtle hover:text-foreground">
                <input type="checkbox" checked={localMode} onChange={(event) => setLocalMode(event.target.checked)} className="size-3.5 accent-primary" />
                本地 Ollama 模式
                {localMode ? <Check className="ml-auto size-3.5 text-success" /> : null}
              </label>
            </RailSection>
          </>
        ) : null}
      </div>

      <div className="shrink-0 border-t border-border p-2">
        <div className="mb-1 grid grid-cols-3 gap-1">
          <Button asChild variant="ghost" size="compact"><Link href="/knowledge"><Library className="size-3.5" />知识</Link></Button>
          <Button asChild variant="ghost" size="compact"><Link href="/tasks"><ListChecks className="size-3.5" />任务</Link></Button>
          {canManage ? <Button asChild variant="ghost" size="compact"><Link href="/admin"><Settings className="size-3.5" />管理</Link></Button> : <span />}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex h-10 w-full items-center gap-2 rounded-[5px] px-2 text-left hover:bg-surface-subtle">
              <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-surface-subtle text-[11px] font-semibold">{user?.display_name?.slice(0, 1).toUpperCase() || "U"}</span>
              <span className="min-w-0 flex-1"><span className="block truncate text-[13px] font-medium">{user?.display_name || "本地用户"}</span><span className="block truncate text-[10px] text-muted-foreground">{user?.email || "兼容模式"}</span></span>
              <MoreHorizontal className="size-4 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="right" align="end" className="w-60">
            <DropdownMenuLabel>{user?.email || "本地兼容模式"}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {canManage ? <DropdownMenuItem onSelect={() => router.push("/admin")}><Settings className="size-4" />账号与工作区设置</DropdownMenuItem> : null}
            <DropdownMenuItem onSelect={logout}><LogOut className="size-4" />退出登录</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );
}
