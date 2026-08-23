"use client";

import Link from "next/link";
import { Menu, PanelLeft, Plus } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { knowledgeBaseFromPath, sessionFromPath, workbenchHref, workbenchViewFromPath, workbenchViews } from "@/lib/workbench";
import { useWorkspaceStore } from "@/stores/workspace-store";

function titleForPath(pathname: string) {
  if (pathname.startsWith("/home")) return "工作台";
  if (pathname === "/knowledge") return "全部知识库";
  if (/^\/knowledge\/[^/]+\/access/.test(pathname)) return "访问权限";
  if (/^\/knowledge\/[^/]+$/.test(pathname)) return "文档管理";
  if (pathname.startsWith("/tasks")) return "后台任务";
  if (pathname.startsWith("/admin")) return "管理";
  return "CogDoc";
}

export function Header() {
  const pathname = usePathname();
  const router = useRouter();
  const openMobile = useWorkspaceStore((state) => state.setMobileSidebarOpen);
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const lastConversation = useWorkspaceStore((state) => state.lastConversation);
  const kbId = knowledgeBaseFromPath(pathname) || selectedKbId;
  const activeView = workbenchViewFromPath(pathname);
  const activeSession = sessionFromPath(pathname);

  const openConversation = () => {
    if (!kbId) return;
    setSelectedKbId(kbId);
    const sessionId = activeSession || (lastConversation?.kbId === kbId ? lastConversation.sessionId : null);
    if (sessionId) {
      router.push(`/knowledge/${encodeURIComponent(kbId)}/chat/${encodeURIComponent(sessionId)}`);
      return;
    }
    router.push(`/knowledge/${encodeURIComponent(kbId)}/chat/${crypto.randomUUID()}`);
  };

  return (
    <header className="flex h-12 shrink-0 items-center border-b border-border bg-surface px-2 md:px-3">
      <Button variant="ghost" size="icon" className="mr-1 md:hidden" onClick={() => openMobile(true)} aria-label="打开导航"><Menu className="size-4" /></Button>
      {kbId ? (
        <>
          <Link href={`/knowledge/${encodeURIComponent(kbId)}`} className="mr-2 hidden max-w-40 shrink-0 items-center gap-1.5 border-r border-border pr-3 text-[12px] font-semibold md:flex" title={kbId}>
            <PanelLeft className="size-3.5 text-muted-foreground" />
            <span className="truncate">{kbId}</span>
          </Link>
          <nav className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto" aria-label="知识库主视图">
            {workbenchViews.map((view) => view.id === "conversation" ? (
              <button
                key={view.id}
                type="button"
                onClick={openConversation}
                className={cn("relative flex h-9 shrink-0 items-center rounded-[4px] px-2.5 text-[12px] font-medium text-muted-foreground hover:bg-surface-subtle hover:text-foreground", activeView === view.id && "text-primary")}
                aria-current={activeView === view.id ? "page" : undefined}
              >
                {view.label}
                {activeView === view.id ? <span className="absolute inset-x-2 -bottom-[7px] h-0.5 bg-primary" /> : null}
              </button>
            ) : (
              <Link
                key={view.id}
                href={`${workbenchHref(view.id, kbId)}?kb=${encodeURIComponent(kbId)}`}
                className={cn("relative flex h-9 shrink-0 items-center rounded-[4px] px-2.5 text-[12px] font-medium text-muted-foreground hover:bg-surface-subtle hover:text-foreground", activeView === view.id && "text-primary")}
                aria-current={activeView === view.id ? "page" : undefined}
              >
                {view.label}
                {activeView === view.id ? <span className="absolute inset-x-2 -bottom-[7px] h-0.5 bg-primary" /> : null}
              </Link>
            ))}
          </nav>
          <Button variant="ghost" size="compact" className="ml-2 hidden shrink-0 sm:flex" onClick={() => {
            if (!kbId) return;
            router.push(`/knowledge/${encodeURIComponent(kbId)}/chat/${crypto.randomUUID()}`);
          }}><Plus className="size-3.5" />新对话</Button>
        </>
      ) : (
        <div className="min-w-0"><h1 className="truncate text-[13px] font-semibold">{titleForPath(pathname)}</h1></div>
      )}
      <div className="ml-auto flex items-center gap-2" id="page-header-actions" />
    </header>
  );
}
