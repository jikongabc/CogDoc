"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, streamChat } from "@/lib/api/client";
import type { ChatMode } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";
import { ChatWindow } from "@/components/ai/chat-window";
import { EvidencePanel } from "@/components/ai/evidence-panel";
import type { MessageView } from "@/components/ai/message";
import { FeedbackControls } from "@/features/feedback/feedback-controls";
import { historyMessage, hydrateHistoryEvidence } from "@/features/chat/history";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { usePermission } from "@/features/auth/permissions";
import { decodeRouteParam } from "@/lib/routing";

function stageLabel(stage?: string) {
  const labels: Record<string, string> = {
    route: "正在理解问题",
    rewrite: "正在改写检索问题",
    retrieve: "正在检索来源",
    rerank: "正在整理证据",
    generate: "正在生成回答",
    verify: "正在核对引用",
    claim_verify: "正在核验声明",
  };
  if (!stage) return "正在处理请求";
  return labels[stage] || "正在处理请求";
}

function ChatSession({ kbId, sessionId }: { kbId: string; sessionId: string }) {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const localModelMode = useWorkspaceStore((state) => state.localModelMode);
  const [liveMessages, setLiveMessages] = useState<MessageView[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const hydrationSeededRef = useRef(false);
  const hydrationQueueRef = useRef<string[]>([]);
  const hydrationQueuedRef = useRef(new Set<string>());
  const hydrationActiveRef = useRef(0);
  const hydrationControllersRef = useRef(new Map<string, AbortController>());
  const pumpHydrationRef = useRef<() => void>(() => undefined);
  const closeEvidence = useWorkspaceStore((state) => state.closeEvidence);
  const historyKey = useMemo(() => queryKeys.history(workspaceId, kbId, sessionId), [workspaceId, kbId, sessionId]);
  const history = useQuery({
    queryKey: historyKey,
    queryFn: async () => {
      const result = await api.sessionHistory(kbId, sessionId);
      return result.messages.map((message, index) => historyMessage(message, index, kbId, sessionId));
    },
  });

  useLayoutEffect(() => {
    closeEvidence();
  }, [closeEvidence]);

  const pumpHydration = useCallback(() => {
    while (hydrationActiveRef.current < 4 && hydrationQueueRef.current.length) {
      const messageId = hydrationQueueRef.current.shift();
      if (!messageId) continue;
      const message = queryClient.getQueryData<MessageView[]>(historyKey)?.find((item) => item.id === messageId);
      if (!message || message.evidenceStatus !== "loading") {
        hydrationQueuedRef.current.delete(messageId);
        continue;
      }
      const controller = new AbortController();
      hydrationControllersRef.current.set(messageId, controller);
      hydrationActiveRef.current += 1;
      void hydrateHistoryEvidence(message, kbId, sessionId, controller.signal)
        .then((hydrated) => {
          if (controller.signal.aborted) return;
          queryClient.setQueryData<MessageView[]>(historyKey, (current) => current?.map((item) => item.id === hydrated.id ? hydrated : item));
        })
        .catch(() => undefined)
        .finally(() => {
          hydrationControllersRef.current.delete(messageId);
          hydrationQueuedRef.current.delete(messageId);
          hydrationActiveRef.current = Math.max(0, hydrationActiveRef.current - 1);
          pumpHydrationRef.current();
        });
    }
  }, [historyKey, kbId, queryClient, sessionId]);

  useEffect(() => {
    pumpHydrationRef.current = pumpHydration;
  }, [pumpHydration]);

  const requestEvidenceHydration = useCallback((messageId: string) => {
    if (hydrationQueuedRef.current.has(messageId) || hydrationControllersRef.current.has(messageId)) return;
    hydrationQueuedRef.current.add(messageId);
    hydrationQueueRef.current.push(messageId);
    pumpHydrationRef.current();
  }, []);

  useEffect(() => {
    if (!history.data || hydrationSeededRef.current) return;
    hydrationSeededRef.current = true;
    history.data.filter((message) => message.evidenceStatus === "loading").slice(-4).forEach((message) => requestEvidenceHydration(message.id));
  }, [history.data, requestEvidenceHydration]);

  useEffect(() => () => {
    hydrationQueueRef.current = [];
    hydrationQueuedRef.current.clear();
    hydrationControllersRef.current.forEach((controller) => controller.abort());
    hydrationControllersRef.current.clear();
    hydrationActiveRef.current = 0;
  }, []);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const stop = useCallback(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setStreaming(false);
    setStage("");
    setLiveMessages((current) => current.map((message) => message.streaming ? { ...message, streaming: false, incomplete: true, notInContext: true } : message));
  }, []);

  const send = useCallback(async (prompt: string, mode: ChatMode) => {
    if (controllerRef.current) return;
    const userId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    const controller = new AbortController();
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    controllerRef.current = controller;
    setError(null);
    setStage("正在连接知识库");
    setStreaming(true);
    setLiveMessages((current) => [...current, { id: userId, role: "user", content: prompt }, { id: assistantId, role: "assistant", content: "", query: prompt, streaming: true }]);
    let finalReceived = false;
    try {
      for await (const event of streamChat({ query: prompt, doc_id: kbId, session_id: sessionId, mode, is_local: localModelMode }, controller.signal)) {
        if (generationRef.current !== generation) return;
        if (event.type === "start") setStage("正在理解问题");
        if (event.type === "node") setStage(stageLabel(event.data.stage));
        if (event.type === "token") setLiveMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: message.content + (event.data.content || "") } : message));
        if (event.type === "error") throw new ApiError(500, event.data);
        if (event.type === "final") {
          finalReceived = true;
          const response = event.data;
          setLiveMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: response.answer, response, streaming: false } : message));
        }
      }
      if (!finalReceived) throw new ApiError(502, { error_code: "STREAM_INTERRUPTED", message: "响应在完成前中断，请重试。" });
    } catch (errorValue) {
      if (controller.signal.aborted || generationRef.current !== generation) return;
      const message = errorValue instanceof Error ? errorValue.message : "请求失败";
      setError(message);
      setLiveMessages((current) => current.map((item) => item.id === assistantId ? { ...item, streaming: false, incomplete: Boolean(item.content), notInContext: true } : item));
    } finally {
      if (controllerRef.current === controller && generationRef.current === generation) {
        controllerRef.current = null;
        setStreaming(false);
        setStage("");
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.sessions(workspaceId, kbId) });
    }
  }, [kbId, localModelMode, queryClient, sessionId, workspaceId]);

  const messages = useMemo(() => [...(history.data ?? []), ...liveMessages], [history.data, liveMessages]);
  const responses = useMemo(() => messages.flatMap((message) => message.response ? [message.response] : []), [messages]);
  return (
    <div className="relative flex h-[calc(100dvh-48px)] min-h-0 flex-col overflow-hidden lg:flex-row">
      <ChatWindow kbId={kbId} messages={messages} streaming={streaming} historyLoading={history.isPending} stage={stage} error={error || (history.isError ? "无法读取历史消息，你仍可继续当前对话。" : null)} onSend={send} onStop={stop} onEvidenceVisible={requestEvidenceHydration} renderFooter={(message) => message.response ? <FeedbackControls kbId={kbId} sessionId={sessionId} query={message.query || ""} response={message.response} /> : null} />
      <EvidencePanel responses={responses} />
    </div>
  );
}

export default function ChatPage() {
  const params = useParams<{ kbId: string; sessionId: string }>();
  const kbId = decodeRouteParam(params.kbId);
  const sessionId = decodeRouteParam(params.sessionId);
  const canQuery = usePermission("query");
  if (!canQuery) return <div className="mx-auto max-w-xl p-6"><div className="border-l-2 border-warning bg-warning-subtle px-4 py-3 text-sm text-warning"><p className="font-medium">当前角色不能发起知识库对话</p><p className="mt-1 text-xs">需要 Query 权限。你仍可返回知识库查看有权访问的文档。</p></div></div>;
  return <ChatSession key={`${kbId}:${sessionId}`} kbId={kbId} sessionId={sessionId} />;
}
