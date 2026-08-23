"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bot, User } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ChatResponse, CitationLedgerEntry } from "@/lib/api/types";
import { Citation } from "./citation";
import { sourceLocation } from "./source-preview";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

function codePointSlice(value: string, start: number, end?: number) {
  return Array.from(value).slice(start, end).join("");
}

function answerWithEvidenceLinks(answer: string, ledger: CitationLedgerEntry[]) {
  const occurrences = ledger.flatMap((entry, ledgerIndex) => entry.occurrences.map((occurrence) => ({ ...occurrence, entry, ledgerIndex }))).sort((a, b) => a.answer_start - b.answer_start);
  if (!occurrences.length) return answer;
  let cursor = 0;
  let output = "";
  for (const occurrence of occurrences) {
    if (occurrence.answer_start < cursor) continue;
    output += codePointSlice(answer, cursor, occurrence.answer_start);
    output += `[${occurrence.ledgerIndex + 1}](#evidence-${occurrence.entry.evidence_id})`;
    cursor = occurrence.answer_end;
  }
  return output + codePointSlice(answer, cursor);
}

export interface MessageView {
  id: string;
  role: "user" | "assistant";
  content: string;
  query?: string;
  response?: ChatResponse;
  streaming?: boolean;
  incomplete?: boolean;
  notInContext?: boolean;
  evidenceStatus?: "loading" | "unavailable";
}

export function Message({ message, footer, onEvidenceVisible }: { message: MessageView; footer?: React.ReactNode; onEvidenceVisible?: (messageId: string) => void }) {
  const response = message.response;
  const rendered = response ? answerWithEvidenceLinks(response.answer, response.citation_ledger) : message.content;
  const articleRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const article = articleRef.current;
    if (!article || message.evidenceStatus !== "loading" || !onEvidenceVisible) return;
    const root = article.closest<HTMLElement>("[data-chat-viewport]");
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        onEvidenceVisible(message.id);
        observer.disconnect();
      }
    }, { root, rootMargin: "240px 0px" });
    observer.observe(article);
    return () => observer.disconnect();
  }, [message.evidenceStatus, message.id, onEvidenceVisible]);
  return (
    <article ref={articleRef} className={cn("chat-message border-b border-border", message.role === "user" ? "bg-surface-subtle/55" : "bg-surface")}>
      <div className="mx-auto flex max-w-[800px] gap-3 px-4 py-5 md:px-6">
        <span className={cn("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-[4px]", message.role === "assistant" ? "bg-foreground text-white" : "border border-border bg-surface text-muted-foreground")}>{message.role === "assistant" ? <Bot className="size-3.5" /> : <User className="size-3.5" />}</span>
        <div className="min-w-0 flex-1"><div className="mb-1.5 flex items-center gap-2 text-xs font-medium"><span>{message.role === "assistant" ? "CogDoc" : "你"}</span>{response?.is_valid && !message.evidenceStatus ? <Badge variant="success">已校验</Badge> : null}{response && !response.is_valid && !message.evidenceStatus ? <Badge variant="warning">需要审核</Badge> : null}{message.evidenceStatus === "loading" ? <Badge>正在恢复证据</Badge> : null}{message.evidenceStatus === "unavailable" ? <Badge variant="warning">证据暂不可用</Badge> : null}{message.incomplete ? <Badge variant="warning">未完成</Badge> : null}</div>
          <div className="answer-markdown max-w-none break-words text-[15px] leading-[25px] text-foreground">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href, children }) => {
                  if (href?.startsWith("#evidence-") && response) {
                    const evidenceId = href.slice("#evidence-".length);
                    const index = response.citation_ledger.findIndex((entry) => entry.evidence_id === evidenceId);
                    const entry = response.citation_ledger[index];
                    if (entry) return <Citation traceId={response.trace_id} evidenceId={evidenceId} index={index + 1} source={entry.source} location={sourceLocation(entry)} />;
                  }
                  return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
                },
              }}
            >{rendered || (message.streaming ? "正在整理证据…" : "（无内容）")}</ReactMarkdown>
          </div>
          {message.streaming ? <span className="mt-1 inline-block h-4 w-0.5 animate-pulse bg-primary" aria-hidden="true" /> : null}
          {response && !response.is_valid && !message.evidenceStatus ? <p className="mt-2 border-l-2 border-warning bg-warning-subtle px-3 py-2 text-xs text-warning">回答未通过引用或声明校验，请在使用前核对右侧证据和原始来源。</p> : null}
          {message.notInContext ? <p className="mt-2 border-l-2 border-warning bg-warning-subtle px-3 py-2 text-xs text-warning">这次生成未写入会话，不会参与后续问题的上下文。需要引用它时，请重新发送问题。</p> : null}
          {footer ? <div className="mt-3">{footer}</div> : null}
        </div>
      </div>
    </article>
  );
}
