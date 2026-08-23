"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileArchive, Plus, ScrollText, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { AdminPageFrame } from "@/components/admin/admin-nav";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiDownload } from "@/lib/api/client";
import { controlApi, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";

export default function AuditPage() {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.workspace?.workspace_id || "");
  const events = useQuery({
    queryKey: ["admin", "audit-events"],
    queryFn: () => controlApi.auditEvents(250),
    enabled: Boolean(workspaceId),
    refetchInterval: 15_000,
  });
  const exportsQuery = useQuery({
    queryKey: ["admin", "audit-exports"],
    queryFn: () => controlApi.auditExports(),
    enabled: Boolean(workspaceId),
    refetchInterval: 10_000,
  });
  const eventRows = records(events.data, ["items", "events"]);
  const exportRows = records(exportsQuery.data, ["items", "jobs", "exports"]);

  const create = useMutation({
    mutationFn: () => controlApi.createAuditExport({ from_sequence: null, to_sequence: null, actions: [], statuses: [], retention_seconds: 86400 }),
    onSuccess: async () => {
      toast.success("审计导出已创建");
      await queryClient.invalidateQueries({ queryKey: ["admin", "audit-exports"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const remove = useMutation({
    mutationFn: (row: JsonRecord) => controlApi.deleteAuditExport(textValue(row.job_id, textValue(row.id, "")), numberValue(row.revision)),
    onSuccess: async () => {
      toast.success("审计导出已删除");
      await queryClient.invalidateQueries({ queryKey: ["admin", "audit-exports"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const download = useMutation({
    mutationFn: async (id: string) => ({ id, blob: await apiDownload(`/audit-events/exports/${encodeURIComponent(id)}/content`) }),
    onSuccess: ({ id, blob }) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `cogdoc-audit-${id}.jsonl`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success("审计导出已下载");
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <AdminPageFrame>
      <PageHeader
        eyebrow="Compliance ledger"
        title="审计与导出"
        description="检查工作区审计链，并创建具有保留期的合规导出。"
        actions={workspaceId ? <Button variant="primary" onClick={() => create.mutate()} loading={create.isPending}><Plus className="size-4" />创建导出</Button> : undefined}
      />
      {!workspaceId ? (
        <EmptyState icon={ScrollText} title="本地模式没有工作区审计目录" description="启用账号认证后，可按工作区检查审计事件并创建受控导出。" />
      ) : (
        <div className="p-4 md:p-6">
          <Tabs defaultValue="events">
            <TabsList className="mb-4">
              <TabsTrigger value="events">审计事件 <Badge className="ml-1">{eventRows.length}</Badge></TabsTrigger>
              <TabsTrigger value="exports">导出作业 <Badge className="ml-1">{exportRows.length}</Badge></TabsTrigger>
            </TabsList>
            <TabsContent value="events">
              <section className="overflow-hidden border border-border bg-surface">
                <QueryState pending={events.isPending} error={events.error} onRetry={() => void events.refetch()} />
                {events.data && !eventRows.length ? (
                  <EmptyState icon={ScrollText} compact title="没有审计事件" description="受保护的读取和变更会按后端审计策略记录。" />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[900px] text-left text-[12px]">
                      <thead className="border-b border-border bg-surface-subtle text-[11px] text-muted-foreground">
                        <tr><th className="px-3 py-2 font-medium">序号</th><th className="px-3 py-2 font-medium">操作者</th><th className="px-3 py-2 font-medium">动作</th><th className="px-3 py-2 font-medium">资源</th><th className="px-3 py-2 font-medium">结果</th><th className="px-3 py-2 font-medium">时间</th></tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {eventRows.map((row, index) => (
                          <tr key={textValue(row.event_id, textValue(row.sequence, String(index)))} className="hover:bg-surface-subtle">
                            <td className="px-3 py-2.5 font-mono text-[10px] text-muted-foreground">{textValue(row.sequence)}</td>
                            <td className="px-3 py-2.5">{textValue(row.subject_id, textValue(row.actor_id))}</td>
                            <td className="px-3 py-2.5 font-medium">{textValue(row.action, textValue(row.method))}</td>
                            <td className="max-w-xs truncate px-3 py-2.5 font-mono text-[10px] text-muted-foreground">{textValue(row.resource, textValue(row.path))}</td>
                            <td className="px-3 py-2.5"><StatusBadge status={numberValue(row.status, 200) < 400 ? "succeeded" : "failed"} label={textValue(row.status)} /></td>
                            <td className="px-3 py-2.5 font-mono text-[10px] text-muted-foreground">{textValue(row.created_at, textValue(row.timestamp))}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </TabsContent>
            <TabsContent value="exports">
              <section className="overflow-hidden border border-border bg-surface">
                <QueryState pending={exportsQuery.isPending} error={exportsQuery.error} onRetry={() => void exportsQuery.refetch()} />
                {exportsQuery.data && !exportRows.length ? (
                  <EmptyState icon={FileArchive} compact title="没有审计导出" description="创建导出后，服务端会生成带到期时间的合规文件。" />
                ) : (
                  <div className="divide-y divide-border">
                    {exportRows.map((row) => {
                      const id = textValue(row.job_id, textValue(row.id));
                      const ready = textValue(row.status, "") === "succeeded";
                      return (
                        <div key={id} className="grid grid-cols-[minmax(0,1fr)_120px_160px_auto] items-center gap-3 px-4 py-3 text-[13px]">
                          <div><p className="font-medium">审计事件导出</p><p className="font-mono text-[10px] text-muted-foreground">{id}</p></div>
                          <StatusBadge status={textValue(row.status, "pending")} />
                          <span className="font-mono text-[10px] text-muted-foreground">到期 {textValue(row.expires_at)}</span>
                          <div className="flex gap-1">
                            {ready ? <Button variant="ghost" size="icon" loading={download.isPending && download.variables === id} onClick={() => download.mutate(id)} aria-label="下载审计导出"><Download className="size-4" /></Button> : null}
                            <Button variant="ghost" size="icon" className="text-error" onClick={() => remove.mutate(row)} aria-label="删除导出"><Trash2 className="size-4" /></Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            </TabsContent>
          </Tabs>
        </div>
      )}
    </AdminPageFrame>
  );
}
