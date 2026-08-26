"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, CirclePause, CirclePlay, ListChecks, RefreshCw, RotateCcw, Square } from "lucide-react";
import { toast } from "sonner";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usePermission } from "@/features/auth/permissions";
import { ApiError, api } from "@/lib/api/client";
import { controlApi, isRecord, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";

type TaskKind = "all" | "index" | "sync" | "research" | "system" | "exports";
type TaskRow = JsonRecord & { _kind: Exclude<TaskKind, "all"> };
type TaskAction = "cancel" | "pause" | "replay" | "resume" | "start";

const kindLabels: Record<Exclude<TaskKind, "all">, string> = {
  index: "解析与索引",
  sync: "外部同步",
  research: "Research",
  system: "系统作业",
  exports: "审计导出",
};

function rowStatus(row: TaskRow) {
  return textValue(row.status, "unknown").toLowerCase();
}

function rowId(row: TaskRow) {
  return textValue(row.job_id, textValue(row.id, textValue(row.export_id, "")));
}

function rowAction(row: TaskRow): TaskAction | null {
  const status = rowStatus(row);
  if (row._kind === "sync" && status === "dead_letter") return "replay";
  if (row._kind === "research" && status === "paused") return "resume";
  if (row._kind === "research" && status === "running") return "pause";
  if (row._kind === "research" && ["draft", "pending", "planned"].includes(status)) return "start";
  if (row._kind === "system" && status === "dead_letter") return "replay";
  if (row._kind === "system" && ["queued", "running"].includes(status)) return "cancel";
  return null;
}

function actionLabel(action: TaskAction) {
  return { cancel: "取消任务", pause: "暂停任务", replay: "重放任务", resume: "恢复任务", start: "启动任务" }[action];
}

function ActionIcon({ action }: { action: TaskAction }) {
  if (action === "replay") return <RotateCcw className="size-3.5" />;
  if (action === "pause") return <CirclePause className="size-3.5" />;
  if (action === "cancel") return <Square className="size-3.5" />;
  return <CirclePlay className="size-3.5" />;
}

function taskTimestamp(row: TaskRow) {
  const raw = row.updated_at ?? row.finished_at ?? row.created_at;
  if (typeof raw === "number") return raw < 10_000_000_000 ? raw * 1000 : raw;
  const value = textValue(raw, "");
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function taskTime(row: TaskRow) {
  const timestamp = taskTimestamp(row);
  if (timestamp) return new Date(timestamp).toLocaleString("zh-CN", { hour12: false });
  return textValue(row.updated_at ?? row.finished_at ?? row.created_at, "—");
}

async function compatibleIndexJobs(workspaceId: string | null) {
  try {
    return await controlApi.indexJobs();
  } catch (error) {
    // Older CogDoc APIs only expose single-job polling. Until the process is
    // restarted on the aggregate-list route, an empty history is more useful
    // than presenting an unsupported optional endpoint as an outage.
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    const knowledgeBases = await api.knowledgeBases();
    const jobIds = new Set<string>();
    if (typeof window !== "undefined") {
      for (const knowledgeBase of knowledgeBases) {
        const jobId = sessionStorage.getItem(
          `cogdoc.index-job.v1:${workspaceId || "legacy"}:${knowledgeBase.kb_id}`,
        );
        if (jobId) jobIds.add(jobId);
      }
    }
    const results = await Promise.allSettled(
      [...jobIds].map((jobId) => api.indexJob(jobId)),
    );
    return {
      jobs: results.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      ),
    };
  }
}

async function compatibleWorkspaceSyncJobs() {
  try {
    return await controlApi.workspaceSyncJobs();
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    const knowledgeBases = await api.knowledgeBases();
    if (!knowledgeBases.length) return { jobs: [] };
    const results = await Promise.allSettled(
      knowledgeBases.map((knowledgeBase) => controlApi.syncJobs(knowledgeBase.kb_id)),
    );
    const jobs = results.flatMap((result) =>
      result.status === "fulfilled" ? records(result.value, ["jobs", "items"]) : [],
    );
    if (!jobs.length && results.every((result) => result.status === "rejected")) {
      const rejection = results.find(
        (result): result is PromiseRejectedResult => result.status === "rejected",
      );
      throw rejection?.reason ?? new Error("无法读取知识库同步任务");
    }
    return { jobs };
  }
}

async function optionalHaJobs() {
  try {
    return await controlApi.haJobs();
  } catch (error) {
    if (error instanceof ApiError && error.status === 503) return { jobs: [], unavailable: true };
    throw error;
  }
}

export default function TasksPage() {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  const canReadAudit = usePermission("manage_access");
  const canWrite = usePermission("write");
  const [kind, setKind] = useState<TaskKind>("all");
  const index = useQuery({ queryKey: ["tasks", workspaceId, "index"], queryFn: () => compatibleIndexJobs(workspaceId), retry: false, refetchInterval: 10_000 });
  const sync = useQuery({ queryKey: ["tasks", workspaceId, "sync"], queryFn: compatibleWorkspaceSyncJobs, retry: false, refetchInterval: 10_000 });
  const research = useQuery({ queryKey: ["tasks", workspaceId, "research"], queryFn: () => controlApi.researchSummaries(), retry: false, refetchInterval: 10_000 });
  const ha = useQuery({ queryKey: ["tasks", workspaceId, "ha"], queryFn: optionalHaJobs, enabled: canReadAudit, retry: false, refetchInterval: (query) => isRecord(query.state.data) && query.state.data.unavailable === true ? false : 10_000 });
  const exportsQuery = useQuery({ queryKey: ["tasks", workspaceId, "exports"], queryFn: () => controlApi.auditExports(), enabled: canReadAudit, retry: false, refetchInterval: 10_000 });

  const allRows = useMemo<TaskRow[]>(() => {
    const tagged = (value: unknown, keys: string[], rowKind: Exclude<TaskKind, "all">) => records(value, keys).map((row) => ({ ...row, _kind: rowKind }));
    return [
      ...tagged(index.data, ["jobs", "items"], "index"),
      ...tagged(sync.data, ["jobs", "items"], "sync"),
      ...tagged(research.data, ["jobs", "items", "summaries"], "research"),
      ...tagged(ha.data, ["jobs", "items"], "system"),
      ...tagged(exportsQuery.data, ["exports", "jobs", "items"], "exports"),
    ].sort((a, b) => taskTimestamp(b) - taskTimestamp(a));
  }, [exportsQuery.data, ha.data, index.data, research.data, sync.data]);

  const visible = kind === "all" ? allRows : allRows.filter((row) => row._kind === kind);
  const haUnavailable = isRecord(ha.data) && ha.data.unavailable === true;
  const queries = [index, sync, research, ...(canReadAudit ? [ha, exportsQuery] : [])];
  const isFetching = queries.some((query) => query.isFetching);
  const pending = !allRows.length && queries.some((query) => query.isPending);
  const failureCandidates: [string, Error | null][] = [
    ["解析与索引", index.error],
    ["外部同步", sync.error],
    ["Research", research.error],
    ...(canReadAudit ? [["系统作业", ha.error] as [string, Error | null]] : []),
    ...(canReadAudit ? [["审计导出", exportsQuery.error] as [string, Error | null]] : []),
  ];
  const failures = failureCandidates.flatMap(([name, error]) => error ? [[name, error] as const] : []);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["tasks"] });

  const action = useMutation({
    mutationFn: async ({ row, name }: { row: TaskRow; name: TaskAction }) => {
      const id = rowId(row);
      if (row._kind === "sync" && name === "replay") return controlApi.replaySyncJob(textValue(row.kb_id, ""), id);
      if (row._kind === "research" && ["start", "pause", "resume"].includes(name)) return controlApi.researchAction(id, name);
      if (row._kind === "system" && name === "replay") return controlApi.replayHaJob(id, crypto.randomUUID());
      if (row._kind === "system" && name === "cancel") return controlApi.cancelHaJob(id);
      throw new Error("当前任务没有可用操作");
    },
    onSuccess: async (_, variables) => {
      toast.success(`${actionLabel(variables.name)}已提交`);
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (error) => toast.error(error.message),
  });

  const label = (row: TaskRow) => textValue(row.title, textValue(row.queue, textValue(row.connection_name, row._kind === "index" ? "文档解析与索引" : kindLabels[row._kind])));
  const href = (row: TaskRow) => {
    const kbId = textValue(row.kb_id, "");
    if (row._kind === "research") return `/research${kbId ? `?kb=${encodeURIComponent(kbId)}&job=${encodeURIComponent(rowId(row))}` : ""}`;
    if (row._kind === "index" && kbId) return `/knowledge/${encodeURIComponent(kbId)}`;
    if (row._kind === "sync" && kbId) return `/integrations?kb=${encodeURIComponent(kbId)}&tab=jobs`;
    if (row._kind === "exports") return "/admin/audit";
    return "";
  };

  return (
    <div className="min-h-full">
      <PageHeader eyebrow="Operations" title="任务" description="统一查看解析、切块、索引、同步、Research、审计导出和 HA 作业的真实状态。" actions={<Button variant="secondary" onClick={refresh} loading={isFetching}><RefreshCw className="size-4" />刷新</Button>} />
      <div className="border-b border-border bg-surface px-5"><Tabs value={kind} onValueChange={(value) => setKind(value as TaskKind)}><TabsList><TabsTrigger value="all">全部 <Badge className="ml-1">{allRows.length}</Badge></TabsTrigger><TabsTrigger value="index">入库</TabsTrigger><TabsTrigger value="sync">同步</TabsTrigger><TabsTrigger value="research">研究</TabsTrigger>{canReadAudit ? <><TabsTrigger value="system">系统</TabsTrigger><TabsTrigger value="exports">导出</TabsTrigger></> : null}</TabsList></Tabs></div>
      <div className="mx-auto max-w-[1240px] p-4 md:p-6">
        {failures.length ? <div role="alert" className="mb-3 flex items-start gap-3 border-l-2 border-warning bg-warning-subtle px-3 py-2.5 text-xs"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" /><div><p className="font-medium">部分任务暂时无法读取</p><ul className="mt-1 space-y-0.5 text-muted-foreground">{failures.map(([name, error]) => <li key={name}>{name}：{error.message}</li>)}</ul></div><Button variant="ghost" size="compact" className="ml-auto" onClick={refresh}>重试</Button></div> : null}
        <div className="overflow-hidden border border-border bg-surface">
          <QueryState pending={pending} label="正在读取任务" />
          {!pending && !visible.length ? <EmptyState icon={ListChecks} compact title={kind === "system" && haUnavailable ? "当前未启用 HA 作业队列" : failures.length ? "其余任务为空" : "没有匹配的任务"} description={kind === "system" && haUnavailable ? "当前部署按单节点模式运行；启用 HA 后，镜像、迁移和恢复作业会显示在这里。" : failures.length ? "已展示可用数据；失败的任务来源可通过上方提示重试。" : "上传、同步或研究任务开始后，会在这里显示服务端状态和所属资源。"} /> : null}
          {visible.length ? <div className="overflow-x-auto"><table className="w-full min-w-[880px] text-left text-[13px]"><thead className="border-b border-border bg-surface-subtle text-[11px] text-muted-foreground"><tr><th className="px-3 py-2 font-medium">任务</th><th className="px-3 py-2 font-medium">类型</th><th className="px-3 py-2 font-medium">知识库</th><th className="px-3 py-2 font-medium">状态</th><th className="px-3 py-2 font-medium">更新时间</th><th className="w-24 px-3 py-2 text-right font-medium">操作</th></tr></thead><tbody className="divide-y divide-border">{visible.map((row, indexValue) => {
            const taskId = rowId(row) || String(indexValue);
            const status = rowStatus(row);
            const candidateAction = rowAction(row);
            const availableAction = candidateAction && (
              (row._kind === "sync" && canReadAudit)
              || (["research", "system"].includes(row._kind) && canWrite)
            ) ? candidateAction : null;
            const taskHref = href(row);
            const actionPending = action.isPending && rowId(action.variables?.row ?? row) === taskId;
            const errorMessage = textValue(row.error_message, "");
            return <tr key={`${row._kind}-${taskId}`} className="hover:bg-surface-subtle"><td className="px-3 py-2.5"><p className="font-medium">{label(row)}</p><p className="mt-0.5 max-w-sm truncate font-mono text-[10px] text-muted-foreground">{taskId}</p>{errorMessage ? <p className="mt-1 max-w-sm truncate text-[11px] text-error" title={errorMessage}>{errorMessage === "provider HTTP 401" ? "提供方认证失败（HTTP 401），请检查或轮换凭据。" : errorMessage}</p> : null}</td><td className="px-3 py-2.5 text-muted-foreground">{kindLabels[row._kind]}</td><td className="px-3 py-2.5 text-muted-foreground">{textValue(row.kb_id)}</td><td className="px-3 py-2.5"><StatusBadge status={status} /></td><td className="px-3 py-2.5 font-mono text-[11px] text-muted-foreground">{taskTime(row)}</td><td className="px-3 py-2.5"><div className="flex justify-end gap-1">{availableAction ? <Button variant="ghost" size="icon" loading={actionPending} disabled={action.isPending} onClick={() => action.mutate({ row, name: availableAction })} aria-label={actionLabel(availableAction)}><ActionIcon action={availableAction} /></Button> : null}{taskHref ? <Button asChild variant="ghost" size="icon"><Link href={taskHref} aria-label="打开所属资源"><ArrowUpRight className="size-3.5" /></Link></Button> : null}</div></td></tr>;
          })}</tbody></table></div> : null}
        </div>
      </div>
    </div>
  );
}
