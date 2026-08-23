"use client";

import { useMemo, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cable, DatabaseZap, Download, KeyRound, Play, Plus, RefreshCw, RotateCcw, Route, Trash2, Unplug } from "lucide-react";
import { useParams } from "next/navigation";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { apiDownload } from "@/lib/api/client";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { decodeRouteParam } from "@/lib/routing";
import { cn } from "@/lib/utils";

const connectionSchema = z.object({ type: z.enum(["local-directory", "git", "url", "zotero", "notion", "confluence", "sharepoint", "s3"]), name: z.string().trim().min(1).max(160), config: z.string().min(2), credentialId: z.string(), visible: z.boolean() });
const credentialSchema = z.object({ provider: z.string().trim().min(1), label: z.string().trim().min(1), secrets: z.string().min(2) });

function parseObject(value: string) {
  const parsed: unknown = JSON.parse(value);
  if (!isRecord(parsed)) throw new Error("配置必须是 JSON 对象");
  return parsed;
}

function ConnectionDialog({ kbId }: { kbId: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof connectionSchema>>({ resolver: zodResolver(connectionSchema), defaultValues: { type: "url", name: "", config: "{\n  \"urls\": []\n}", credentialId: "", visible: false } });
  const connectorType = useWatch({ control: form.control, name: "type" });
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof connectionSchema>) => controlApi.createConnection(kbId, { connector_type: values.type, name: values.name, config: parseObject(values.config), credential_id: values.credentialId || undefined, workspace_visible: values.visible, secret_env: {} }), onSuccess: async () => { toast.success("连接器已创建"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => form.setError("root", { message: error.message }) });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />添加连接器</Button></DialogTrigger><DialogContent className="max-w-xl"><DialogHeader><DialogTitle>添加来源连接器</DialogTitle><DialogDescription>选择现有提供方并填写其原生配置。密钥应单独保存在加密凭据库。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-1.5"><Label>连接器类型</Label><Select value={connectorType} onValueChange={(value) => form.setValue("type", value as z.infer<typeof connectionSchema>["type"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["local-directory", "git", "url", "zotero", "notion", "confluence", "sharepoint", "s3"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor="connection-name">显示名称</Label><Input id="connection-name" {...form.register("name")} /></div><div className="space-y-1.5 sm:col-span-2"><Label htmlFor="connection-config">连接器配置（JSON）</Label><Textarea id="connection-config" className="min-h-40 font-mono text-xs" spellCheck={false} {...form.register("config")} /><p className="text-[11px] text-muted-foreground">字段严格沿用后端连接器配置，不在浏览器中改写路径或 URL。</p></div><div className="space-y-1.5"><Label htmlFor="credential-id">凭据 ID（可选）</Label><Input id="credential-id" {...form.register("credentialId")} /></div><label className="flex items-center gap-2 self-end pb-2 text-[13px]"><input type="checkbox" className="size-4 accent-primary" {...form.register("visible")} />工作区可见</label>{form.formState.errors.root ? <p className="text-xs text-error sm:col-span-2">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>保存连接器</Button></DialogFooter></form></DialogContent></Dialog>;
}

function CredentialDialog({ kbId }: { kbId: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof credentialSchema>>({ resolver: zodResolver(credentialSchema), defaultValues: { provider: "notion", label: "", secrets: "{\n  \"token\": \"\"\n}" } });
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof credentialSchema>) => controlApi.createConnectorCredential(kbId, { provider: values.provider, credential_kind: "static", label: values.label, secret_values: parseObject(values.secrets), scopes: [] }), onSuccess: async () => { toast.success("加密凭据已保存"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources", "credentials"] }); }, onError: (error) => form.setError("root", { message: error.message }) });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><KeyRound className="size-4" />添加凭据</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>添加加密凭据</DialogTitle><DialogDescription>密钥只发送到现有后端凭据库；列表只返回字段名和元数据。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><Label htmlFor="provider">提供方</Label><Input id="provider" {...form.register("provider")} /></div><div className="space-y-1.5"><Label htmlFor="credential-label">标签</Label><Input id="credential-label" {...form.register("label")} /></div></div><div className="space-y-1.5"><Label htmlFor="secret-values">密钥字段（JSON）</Label><Textarea id="secret-values" className="min-h-32 font-mono text-xs" spellCheck={false} {...form.register("secrets")} /></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>加密保存</Button></DialogFooter></form></DialogContent></Dialog>;
}

function OAuthDialog({ kbId, connections }: { kbId: string; connections: JsonRecord[] }) {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("notion");
  const [connectionId, setConnectionId] = useState("");
  const mutation = useMutation({
    mutationFn: async () => {
      const value = await controlApi.authorizeConnectorOauth(kbId, provider, connectionId || undefined);
      const row = isRecord(value) ? value : {};
      const authorizationUrl = textValue(row.authorization_url, "");
      if (!authorizationUrl.startsWith("https://")) throw new Error("OAuth 服务返回了无效授权地址");
      return authorizationUrl;
    },
    onSuccess: (authorizationUrl) => {
      window.location.assign(authorizationUrl);
    },
    onError: (error) => toast.error(error.message),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Route className="size-4" />OAuth 授权</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>连接提供方账号</DialogTitle><DialogDescription>建立一次性 OAuth 授权会话；令牌由后端加密保存。</DialogDescription></DialogHeader><div className="space-y-4"><div className="space-y-1.5"><Label>提供方</Label><Select value={provider} onValueChange={setProvider}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["notion", "atlassian", "microsoft"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label>授权后绑定连接（可选）</Label><Select value={connectionId || "unbound"} onValueChange={(value) => setConnectionId(value === "unbound" ? "" : value)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unbound">暂不绑定</SelectItem>{connections.map((row) => <SelectItem key={textValue(row.connection_id)} value={textValue(row.connection_id)}>{textValue(row.name)}</SelectItem>)}</SelectContent></Select></div></div><DialogFooter><Button variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button variant="primary" onClick={() => mutation.mutate()} loading={mutation.isPending}>前往授权</Button></DialogFooter></DialogContent></Dialog>;
}

function RotateCredentialDialog({ kbId, credential }: { kbId: string; credential: JsonRecord }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const id = textValue(credential.credential_id, "");
  const form = useForm<{ secrets: string }>({ defaultValues: { secrets: "{}" } });
  const mutation = useMutation({
    mutationFn: ({ secrets }: { secrets: string }) => controlApi.rotateConnectorCredential(kbId, id, { secret_values: parseObject(secrets), expected_revision: numberValue(credential.revision, 1) }),
    onSuccess: async () => { toast.success("凭据已原子轮换"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources", "credentials"] }); },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="轮换凭据"><KeyRound className="size-4" /></Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>轮换加密凭据</DialogTitle><DialogDescription>填写完整的新密钥字段。明文不会在响应或页面状态中回显。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-1.5"><Label htmlFor={`rotate-${id}`}>新密钥字段（JSON）</Label><Textarea id={`rotate-${id}`} className="min-h-32 font-mono text-xs" {...form.register("secrets")} />{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>确认轮换</Button></DialogFooter></form></DialogContent></Dialog>;
}

export default function SourcesPage() {
  const params = useParams<{ kbId: string }>();
  const kbId = decodeRouteParam(params.kbId);
  const queryClient = useQueryClient();
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const connections = useQuery({ queryKey: ["sources", "connections", kbId], queryFn: () => controlApi.connections(kbId) });
  const health = useQuery({ queryKey: ["sources", "health", kbId], queryFn: () => controlApi.connectionHealth(kbId), refetchInterval: 10_000 });
  const jobs = useQuery({ queryKey: ["sources", "jobs", kbId], queryFn: () => controlApi.syncJobs(kbId), refetchInterval: 5000 });
  const credentials = useQuery({ queryKey: ["sources", "credentials", kbId], queryFn: () => controlApi.connectorCredentials(kbId), retry: false });
  const credentialEvents = useQuery({ queryKey: ["sources", "credential-events", kbId], queryFn: () => controlApi.connectorCredentialEvents(kbId), retry: false });
  const catalog = useQuery({ queryKey: ["sources", "catalog", kbId], queryFn: () => controlApi.sourceCatalog(kbId, true) });
  const connectionRows = records(connections.data, ["connections", "items"]);
  const healthRows = records(health.data, ["connections", "items"]);
  const jobRows = records(jobs.data, ["jobs", "items"]);
  const credentialRows = records(credentials.data, ["credentials", "items"]);
  const credentialEventRows = records(credentialEvents.data, ["events", "items"]);
  const sourceRows = records(catalog.data, ["sources", "items", "entries"]);
  const activeSourceId = selectedSourceId || textValue(sourceRows[0]?.source_id, "");
  const activeSource = sourceRows.find((row) => textValue(row.source_id, "") === activeSourceId);
  const versions = useQuery({ queryKey: ["sources", "versions", kbId, activeSourceId], queryFn: () => controlApi.sourceVersions(kbId, activeSourceId), enabled: Boolean(activeSourceId) });
  const versionRows = records(versions.data, ["versions", "items"]);
  const healthByConnection = useMemo(() => new Map(healthRows.map((row) => [textValue(row.connection_id, ""), row])), [healthRows]);
  const sync = useMutation({ mutationFn: (id: string) => controlApi.syncConnection(kbId, id), onSuccess: async () => { toast.success("同步任务已启动"); await queryClient.invalidateQueries({ queryKey: ["sources", "jobs"] }); }, onError: (error) => toast.error(error.message) });
  const toggle = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => controlApi.updateConnection(kbId, id, { enabled }), onSuccess: async () => { toast.success("连接器状态已更新"); await queryClient.invalidateQueries({ queryKey: ["sources", "connections"] }); }, onError: (error) => toast.error(error.message) });
  const replay = useMutation({ mutationFn: (id: string) => controlApi.replaySyncJob(kbId, id), onSuccess: async () => { toast.success("失败任务已重放"); await queryClient.invalidateQueries({ queryKey: ["sources", "jobs"] }); }, onError: (error) => toast.error(error.message) });
  const deleteConnection = useMutation({ mutationFn: (id: string) => controlApi.deleteConnection(kbId, id), onSuccess: async () => { toast.success("连接器已删除"); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => toast.error(error.message) });
  const refreshCredential = useMutation({ mutationFn: (row: JsonRecord) => controlApi.refreshConnectorCredential(kbId, textValue(row.credential_id, ""), numberValue(row.revision, 1)), onSuccess: async () => { toast.success("OAuth 凭据已刷新"); await queryClient.invalidateQueries({ queryKey: ["sources", "credentials"] }); }, onError: (error) => toast.error(error.message) });
  const deleteCredential = useMutation({ mutationFn: (row: JsonRecord) => controlApi.deleteConnectorCredential(kbId, textValue(row.credential_id, ""), numberValue(row.revision, 1)), onSuccess: async () => { toast.success("未绑定凭据已删除"); await queryClient.invalidateQueries({ queryKey: ["sources", "credentials"] }); }, onError: (error) => toast.error(error.message) });
  const downloadVersion = useMutation({ mutationFn: async ({ sourceId, versionId, name }: { sourceId: string; versionId: string; name: string }) => ({ name, blob: await apiDownload(`/knowledge-bases/${encodeURIComponent(kbId)}/source-catalog/${encodeURIComponent(sourceId)}/versions/${encodeURIComponent(versionId)}/content`) }), onSuccess: ({ name, blob }) => { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url); toast.success("来源原件已下载"); }, onError: (error) => toast.error(error.message) });
  const refreshAll = () => void queryClient.invalidateQueries({ queryKey: ["sources"] });
  return <div className="min-h-full"><PageHeader eyebrow="Source operations" title="来源与连接器" description="接入外部知识，管理加密凭据、同步任务、来源目录和不可变版本。" actions={<><OAuthDialog kbId={kbId} connections={connectionRows} /><CredentialDialog kbId={kbId} /><ConnectionDialog kbId={kbId} /></>} /><div className="p-4 md:p-6"><Tabs defaultValue="connections"><div className="mb-4 flex items-end justify-between"><TabsList><TabsTrigger value="connections">连接器 <Badge className="ml-1">{connectionRows.length}</Badge></TabsTrigger><TabsTrigger value="catalog">来源目录 <Badge className="ml-1">{sourceRows.length}</Badge></TabsTrigger><TabsTrigger value="jobs">同步任务 <Badge className="ml-1">{jobRows.length}</Badge></TabsTrigger><TabsTrigger value="credentials">凭据 <Badge className="ml-1">{credentialRows.length}</Badge></TabsTrigger><TabsTrigger value="credential-audit">凭据审计</TabsTrigger></TabsList><Button variant="ghost" size="icon" onClick={refreshAll} aria-label="刷新来源数据"><RefreshCw className={cn("size-4", connections.isFetching && "animate-spin")} /></Button></div>
    <TabsContent value="connections"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={connections.isPending} error={connections.error} onRetry={() => void connections.refetch()} />{connections.data && !connectionRows.length ? <EmptyState icon={Cable} compact title="没有来源连接器" description="添加本地目录、Git、URL、Notion、Confluence、SharePoint、Zotero 或 S3。" action={<ConnectionDialog kbId={kbId} />} /> : <div className="divide-y divide-border">{connectionRows.map((row) => { const id = textValue(row.connection_id, ""); const h = healthByConnection.get(id); const enabled = Boolean(row.enabled); return <div key={id} className="grid grid-cols-[minmax(0,1fr)_130px_120px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div className="min-w-0"><div className="flex items-center gap-2"><p className="truncate font-medium">{textValue(row.name)}</p><Badge>{textValue(row.connector_type)}</Badge></div><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{id}</p></div><StatusBadge status={textValue(h?.status, enabled ? "ready" : "disabled")} /><span className="text-xs text-muted-foreground">{textValue(row.credential_source, "无凭据")}</span><div className="flex gap-1"><Button variant="ghost" size="icon" disabled={!enabled} onClick={() => sync.mutate(id)} aria-label="立即同步"><Play className="size-4" /></Button><Button variant="ghost" size="icon" onClick={() => toggle.mutate({ id, enabled: !enabled })} aria-label={enabled ? "停用连接器" : "启用连接器"}><Unplug className="size-4" /></Button><Button variant="ghost" size="icon" className="text-error" onClick={() => deleteConnection.mutate(id)} aria-label="删除连接器"><Trash2 className="size-4" /></Button></div></div>; })}</div>}</section></TabsContent>
    <TabsContent value="catalog"><div className="grid gap-4 xl:grid-cols-[minmax(360px,0.9fr)_minmax(420px,1.1fr)]"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={catalog.isPending} error={catalog.error} onRetry={() => void catalog.refetch()} />{catalog.data && !sourceRows.length ? <EmptyState icon={Route} compact title="来源目录为空" description="连接器完成同步后，会生成可追踪的来源和版本记录。" /> : <div className="divide-y divide-border">{sourceRows.map((row) => { const id = textValue(row.source_id, ""); return <button key={id} onClick={() => setSelectedSourceId(id)} className={cn("w-full px-4 py-3 text-left hover:bg-surface-subtle", activeSourceId === id && "bg-primary-subtle")}><div className="flex items-center justify-between gap-2"><p className="truncate text-[13px] font-medium">{textValue(row.title, textValue(row.name, id))}</p><StatusBadge status={textValue(row.health_status, textValue(row.status, "ready"))} /></div><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{textValue(row.origin_uri, id)}</p></button>; })}</div>}</section><section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">版本航迹</h3><p className="text-[11px] text-muted-foreground">{activeSourceId || "选择来源"}</p></div><QueryState pending={versions.isPending} error={versions.error} onRetry={() => void versions.refetch()} />{versions.data && !versionRows.length ? <EmptyState icon={DatabaseZap} compact title="没有版本记录" description="每次来源内容变化都会形成不可变版本。" /> : <div className="divide-y divide-border">{versionRows.map((row) => { const versionId = textValue(row.version_id); const available = Boolean(row.artifact_available ?? true); return <div key={versionId} className="px-4 py-3"><div className="flex items-center justify-between gap-2"><p className="font-mono text-[11px] font-medium">{versionId}</p><div className="flex items-center gap-1"><StatusBadge status={textValue(row.artifact_status, available ? "ready" : "metadata_only")} />{available ? <Button variant="ghost" size="icon" onClick={() => downloadVersion.mutate({ sourceId: activeSourceId, versionId, name: textValue(activeSource?.display_name, textValue(activeSource?.name, `${activeSourceId}-${versionId}`)) })} aria-label="下载来源版本"><Download className="size-4" /></Button> : null}</div></div><p className="mt-1 text-[11px] text-muted-foreground">{textValue(row.created_at, textValue(row.fetched_at))} · {textValue(row.size_bytes, "未知大小")} bytes</p></div>; })}</div>}</section></div></TabsContent>
    <TabsContent value="jobs"><section className="overflow-hidden border border-border bg-surface">{jobRows.length ? <div className="divide-y divide-border">{jobRows.map((row) => { const id = textValue(row.job_id); const status = textValue(row.status, "pending"); return <div key={id} className="grid grid-cols-[minmax(0,1fr)_110px_160px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.connector_type)} 同步</p><p className="font-mono text-[10px] text-muted-foreground">{id}</p></div><StatusBadge status={status} /><span className="text-xs text-muted-foreground">{textValue(row.documents_fetched, "0")} 文档 · 尝试 {textValue(row.attempt, "0")}</span>{status === "dead_letter" ? <Button size="compact" onClick={() => replay.mutate(id)}><RotateCcw className="size-3.5" />重放</Button> : <span />}</div>; })}</div> : <EmptyState icon={RefreshCw} compact title="没有同步任务" description="从连接器行启动一次同步。" />}</section></TabsContent>
    <TabsContent value="credentials"><section className="overflow-hidden border border-border bg-surface">{credentialRows.length ? <div className="divide-y divide-border">{credentialRows.map((row) => { const bound = Boolean(textValue(row.connection_id, "")); const oauth = textValue(row.credential_kind, "static") === "oauth"; return <div key={textValue(row.credential_id)} className="grid grid-cols-[minmax(0,1fr)_100px_180px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.label)}</p><p className="font-mono text-[10px] text-muted-foreground">{textValue(row.credential_id)}</p></div><Badge>{textValue(row.provider)}</Badge><span className="text-xs text-muted-foreground">字段：{Array.isArray(row.secret_fields) ? row.secret_fields.join(", ") : "已加密"}{bound ? " · 已绑定" : ""}</span><div className="flex gap-1">{oauth ? <Button variant="ghost" size="icon" onClick={() => refreshCredential.mutate(row)} aria-label="刷新 OAuth 凭据"><RefreshCw className="size-4" /></Button> : <RotateCredentialDialog kbId={kbId} credential={row} />}<Button variant="ghost" size="icon" className="text-error" disabled={bound} onClick={() => deleteCredential.mutate(row)} aria-label="删除凭据"><Trash2 className="size-4" /></Button></div></div>; })}</div> : <EmptyState icon={KeyRound} compact title="没有凭据" description="静态密钥会加密保存；OAuth 凭据由授权流程创建。" action={<CredentialDialog kbId={kbId} />} />}</section></TabsContent>
    <TabsContent value="credential-audit"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={credentialEvents.isPending} error={credentialEvents.error} onRetry={() => void credentialEvents.refetch()} />{credentialEvents.data && !credentialEventRows.length ? <EmptyState icon={KeyRound} compact title="没有凭据操作记录" description="创建、轮换、刷新和删除凭据后会留下审计事件。" /> : <div className="divide-y divide-border">{credentialEventRows.map((row, index) => <div key={textValue(row.event_id, String(index))} className="grid grid-cols-[150px_140px_minmax(0,1fr)_160px] gap-3 px-4 py-3 text-[12px]"><span className="font-medium">{textValue(row.action, "unknown")}</span><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.credential_id)}</span><span className="font-mono text-[10px] text-muted-foreground">actor {textValue(row.actor_id)}</span><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.occurred_at, textValue(row.created_at))}</span></div>)}</div>}</section></TabsContent>
  </Tabs></div></div>;
}
