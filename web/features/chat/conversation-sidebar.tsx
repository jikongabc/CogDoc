"use client";

import Link from "next/link";
import { AlertTriangle, FileText, LoaderCircle, MessageSquarePlus, MessagesSquare, RotateCw } from "lucide-react";
import { useState } from "react";
import type { SessionSummary } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

function SessionLinks({ kbId, sessionId, sessions, loading, error, onRetry, onNavigate }: { kbId: string; sessionId: string; sessions: SessionSummary[]; loading: boolean; error: string | null; onRetry: () => void; onNavigate?: () => void }) {
  if (loading) return <div role="status" className="flex items-center justify-center gap-2 px-2 py-6 text-xs text-muted-foreground"><LoaderCircle className="size-4 animate-spin text-primary" />正在读取对话</div>;
  if (error) return <div role="alert" className="mx-1 border-l-2 border-error bg-error-subtle px-3 py-3 text-xs text-error"><div className="flex items-center gap-2 font-medium"><AlertTriangle className="size-4" />无法读取对话</div><button type="button" className="mt-2 inline-flex items-center gap-1 font-medium hover:underline" onClick={onRetry}><RotateCw className="size-3" />重试</button></div>;
  return sessions.length ? (
    <nav className="space-y-0.5" aria-label="最近对话">
      {sessions.map((session) => <Link key={session.session_id} href={`/knowledge/${encodeURIComponent(kbId)}/chat/${encodeURIComponent(session.session_id)}`} onClick={onNavigate} className={cn("block truncate rounded-[5px] px-2 py-2 text-[13px] text-muted-foreground hover:bg-surface-subtle hover:text-foreground", session.session_id === sessionId && "bg-primary-subtle text-primary")}>{session.title || "未命名对话"}<span className="mt-0.5 block text-[11px] opacity-70">{session.message_count} 条消息</span></Link>)}
    </nav>
  ) : <div className="px-2 py-6 text-center text-xs text-muted-foreground"><MessagesSquare className="mx-auto mb-2 size-4" />还没有历史对话</div>;
}

export function ConversationSidebar({ kbId, sessionId, sessions, sessionsLoading, sessionsError, onRetrySessions, onNewChat }: { kbId: string; sessionId: string; sessions: SessionSummary[]; sessionsLoading: boolean; sessionsError: string | null; onRetrySessions: () => void; onNewChat: () => void }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const newChat = () => { setMobileOpen(false); onNewChat(); };
  return (
    <>
      <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-background lg:flex">
        <div className="border-b border-border p-3"><p className="truncate text-[13px] font-semibold">{kbId}</p><Link href={`/knowledge/${encodeURIComponent(kbId)}`} className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"><FileText className="size-3.5" />查看文档</Link><Button variant="secondary" className="mt-3 w-full" onClick={onNewChat}><MessageSquarePlus className="size-4" />新对话</Button></div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2"><p className="px-2 pb-2 pt-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">最近对话</p><SessionLinks kbId={kbId} sessionId={sessionId} sessions={sessions} loading={sessionsLoading} error={sessionsError} onRetry={onRetrySessions} /></div>
      </aside>
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border bg-background px-3 lg:hidden">
        <p className="min-w-0 flex-1 truncate text-xs font-semibold">{kbId}</p>
        <Link href={`/knowledge/${encodeURIComponent(kbId)}`} className="flex h-8 items-center gap-1.5 rounded-[5px] px-2 text-xs text-muted-foreground hover:bg-surface-subtle hover:text-foreground"><FileText className="size-3.5" />文档</Link>
        <Dialog open={mobileOpen} onOpenChange={setMobileOpen}>
          <DialogTrigger asChild><Button variant="secondary" size="compact"><MessagesSquare className="size-3.5" />会话</Button></DialogTrigger>
          <DialogContent className="max-h-[80dvh] overflow-hidden p-0">
            <DialogHeader className="border-b border-border p-4 pr-12"><DialogTitle>对话记录</DialogTitle><DialogDescription>{kbId} · 选择历史会话或开始新对话</DialogDescription></DialogHeader>
            <div className="max-h-[55dvh] overflow-y-auto p-3"><SessionLinks kbId={kbId} sessionId={sessionId} sessions={sessions} loading={sessionsLoading} error={sessionsError} onRetry={onRetrySessions} onNavigate={() => setMobileOpen(false)} /></div>
            <div className="border-t border-border p-3"><Button variant="primary" className="w-full" onClick={newChat}><MessageSquarePlus className="size-4" />新对话</Button></div>
          </DialogContent>
        </Dialog>
      </div>
    </>
  );
}
