"use client";

import { ArrowDown, ArrowUp, BookOpen, Paperclip, Square } from "lucide-react";
import { useReducedMotion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import type { ChatMode } from "@/lib/api/types";
import { Message, type MessageView } from "./message";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { UploadZone } from "@/components/knowledge/upload-zone";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

interface ChatWindowProps {
  kbId: string;
  messages: MessageView[];
  streaming: boolean;
  historyLoading?: boolean;
  stage?: string;
  error?: string | null;
  renderFooter?: (message: MessageView) => React.ReactNode;
  onEvidenceVisible?: (messageId: string) => void;
  onSend: (prompt: string, mode: ChatMode) => void;
  onStop: () => void;
}

export function ChatWindow({ kbId, messages, streaming, historyLoading = false, stage, error, renderFooter, onEvidenceVisible, onSend, onStop }: ChatWindowProps) {
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<ChatMode>("auto");
  const [newContentBelow, setNewContentBelow] = useState(false);
  const reduceMotion = useReducedMotion();
  const viewportRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const followOutputRef = useRef(true);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const frame = requestAnimationFrame(() => {
      if (followOutputRef.current) {
        viewport.scrollTop = viewport.scrollHeight;
        setNewContentBelow(false);
      } else if (messages.length) {
        setNewContentBelow(true);
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [messages, stage]);

  useEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    composer.style.height = "auto";
    composer.style.height = `${Math.min(160, Math.max(52, composer.scrollHeight))}px`;
  }, [prompt]);

  const followLatest = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    followOutputRef.current = true;
    setNewContentBelow(false);
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: reduceMotion ? "auto" : "smooth" });
  };

  const submit = () => {
    const value = prompt.trim();
    if (!value || streaming || historyLoading) return;
    followOutputRef.current = true;
    setPrompt("");
    onSend(value, mode);
  };
  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-surface" aria-label="对话">
      <div
        ref={viewportRef}
        data-chat-viewport
        className="relative min-h-0 flex-1 overflow-y-auto"
        aria-live={streaming ? "off" : "polite"}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const nearBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 96;
          followOutputRef.current = nearBottom;
          if (nearBottom) setNewContentBelow(false);
        }}
      >
        {!messages.length ? <div className="mx-auto flex h-full max-w-[720px] flex-col items-center justify-center px-6 py-12 text-center"><span className="flex size-10 items-center justify-center rounded-[10px] bg-foreground text-white"><BookOpen className="size-[18px]" /></span><h2 className="mt-5 text-[22px] font-semibold tracking-[-0.03em]">从 {kbId} 中获得有证据的答案</h2><p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">回答会绑定到精确来源。点击引用编号即可核对原文与证据位置。</p><div className="mt-7 grid w-full border-t border-border text-left sm:grid-cols-2">{["总结这些文档的核心结论", "比较文档中的方法与限制", "有哪些重要数字和日期？", "哪些问题在资料中没有答案？"].map((item) => <button key={item} className="border-b border-border px-3 py-3 text-[13px] leading-5 transition-colors hover:bg-surface-subtle sm:odd:border-r" onClick={() => setPrompt(item)}>{item}</button>)}</div></div> : messages.map((message) => <Message key={message.id} message={message} footer={renderFooter?.(message)} onEvidenceVisible={onEvidenceVisible} />)}
        {newContentBelow ? <Button variant="secondary" size="compact" className="sticky bottom-3 left-1/2 z-10 -translate-x-1/2 shadow-[var(--shadow-float)]" onClick={followLatest}><ArrowDown className="size-3.5" />查看新回答</Button> : null}
      </div>
      <div className="shrink-0 bg-surface px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3 md:px-6">
        <div className="mx-auto max-w-[800px]">
          {historyLoading ? <div role="status" className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"><Spinner size="sm" className="text-primary" />正在恢复会话</div> : null}
          {stage && streaming ? <div role="status" aria-live="polite" className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"><Spinner size="sm" className="text-primary" />{stage}</div> : null}
          {error ? <div role="alert" className="mb-2 border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{error}</div> : null}
          <div className="rounded-[16px] border border-border-strong bg-surface shadow-[0_4px_18px_rgba(23,23,23,0.07)] transition-[border-color,box-shadow] focus-within:border-foreground/35 focus-within:shadow-[0_5px_22px_rgba(23,23,23,0.09)]">
            <textarea ref={composerRef} aria-label="消息" value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); } }} disabled={streaming || historyLoading} rows={1} placeholder={historyLoading ? "正在恢复会话…" : "询问这个知识库…"} className="max-h-40 min-h-[52px] w-full resize-none overflow-y-auto bg-transparent px-4 pb-2 pt-3.5 text-[15px] leading-6 outline-none placeholder:text-muted-foreground/70 disabled:opacity-60" />
            <div className="flex items-center justify-between gap-2 px-2.5 pb-2"><div className="flex min-w-0 items-center gap-1"><Dialog><DialogTrigger asChild><Button variant="ghost" size="icon" className="size-8" disabled={streaming || historyLoading} aria-label="上传附件"><Paperclip className="size-4" /></Button></DialogTrigger><DialogContent className="max-w-2xl"><DialogHeader><DialogTitle>上传到 {kbId}</DialogTitle><DialogDescription>文件完成解析和索引后，即可在当前知识库的对话中检索。</DialogDescription></DialogHeader><UploadZone kbId={kbId} /></DialogContent></Dialog><Select value={mode} onValueChange={(value) => setMode(value as ChatMode)} disabled={streaming || historyLoading}><SelectTrigger className="h-8 border-0 bg-transparent shadow-none hover:bg-surface-subtle" aria-label="回答模式"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">自动选择</SelectItem><SelectItem value="qa">问答</SelectItem><SelectItem value="summary">摘要</SelectItem><SelectItem value="compare">对比</SelectItem></SelectContent></Select><span className="hidden text-[11px] text-muted-foreground md:inline">Enter 发送 · Shift+Enter 换行</span></div>{streaming ? <Button variant="secondary" size="compact" onClick={onStop}><Square className="size-3" />停止</Button> : <Button variant="primary" size="icon" className="size-8 rounded-[9px]" onClick={submit} disabled={!prompt.trim() || historyLoading} aria-label="发送"><ArrowUp className="size-4" /></Button>}</div>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted-foreground">CogDoc 会校验引用位置；重要结论仍应结合原始来源判断。</p>
        </div>
      </div>
    </section>
  );
}
