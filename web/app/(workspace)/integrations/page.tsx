"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cable, DatabaseZap, Download, KeyRound, Play, Plus, RefreshCw, RotateCcw, Route, Trash2, Unplug } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
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
import { Spinner } from "@/components/ui/spinner";
import { useKnowledgeBases } from "@/features/knowledge/queries";
import { apiDownload } from "@/lib/api/client";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { cn, formatDateTime } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace-store";

const connectionSchema = z.object({ type: z.enum(["local-directory", "git", "url", "zotero", "notion", "confluence", "sharepoint", "s3"]), name: z.string().trim().min(1).max(160), config: z.string().min(2), credentialId: z.string(), visible: z.boolean() });
const credentialSchema = z.object({ provider: z.string().trim().min(1), label: z.string().trim().min(1), secrets: z.string().min(2) });
const integrationTabs = ["connections", "catalog", "jobs", "credentials", "credential-audit"] as const;
const connectorConfigTemplates: Record<z.infer<typeof connectionSchema>["type"], string> = {
  "local-directory": "{\n  \"root\": \"/srv/cogdoc/imports\"\n}",
  git: "{\n  \"repository\": \"/srv/cogdoc/repos/example\",\n  \"ref\": \"main\"\n}",
  url: "{\n  \"urls\": [\n    \"https://docs.example.com\"\n  ]\n}",
  zotero: "{\n  \"library_type\": \"user\",\n  \"library_id\": \"\"\n}",
  notion: "{}",
  confluence: "{\n  \"base_url\": \"https://example.atlassian.net\",\n  \"include_acl\": true\n}",
  sharepoint: "{\n  \"site_id\": \"\",\n  \"drive_id\": \"\",\n  \"include_acl\": true\n}",
  s3: "{\n  \"bucket\": \"\",\n  \"region\": \"\",\n  \"prefix\": \"\"\n}",
};

function parseObject(value: string) {
  const parsed: unknown = JSON.parse(value);
  if (!isRecord(parsed)) throw new Error("配置必须是 JSON 对象");
  return parsed;
}

function connectorErrorMessage(error: Error) {
  if (error.message.includes("connector credentials are missing")) return "该连接器缺少必需凭据。请先添加对应提供方的凭据，再从“凭据”下拉框中选择。";
  if (error.message.includes("outside the server-owned allowlist")) return "该目录不在服务端允许的接入路径中，请联系管理员配置允许目录。";
  if (error.message.includes("host is not server-allowed")) return "该地址不在服务端允许的域名列表中，请联系管理员配置允许域名。";
  return error.message;
}

function ConfirmDeleteButton({ title, description, disabled, loading, onConfirm }: { title: string; description: string; disabled?: boolean; loading?: boolean; onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="ghost" size="icon" className="text-error" disabled={disabled} aria-label={title}><Trash2 className="size-4" /></Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button variant="destructive" loading={loading} onClick={() => { onConfirm(); setOpen(false); }}>确认删除</Button></DialogFooter></DialogContent></Dialog>;
}

function ConnectionDialog({ kbId, credentials }: { kbId: string; credentials: JsonRecord[] }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof connectionSchema>>({ resolver: zodResolver(connectionSchema), defaultValues: { type: "url", name: "", config: "{\n  \"urls\": []\n}", credentialId: "", visible: false } });
  const connectorType = useWatch({ control: form.control, name: "type" });
  const credentialId = useWatch({ control: form.control, name: "credentialId" });
  useEffect(() => {
    form.setValue("config", connectorConfigTemplates[connectorType], { shouldDirty: true, shouldValidate: true });
  }, [connectorType, form]);
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof connectionSchema>) => controlApi.createConnection(kbId, { connector_type: values.type, name: values.name, config: parseObject(values.config), credential_id: values.credentialId || undefined, workspace_visible: values.visible, secret_env: {} }), onSuccess: async () => { toast.success("连接器已创建"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => form.setError("root", { message: connectorErrorMessage(error) }) });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />添加连接器</Button></DialogTrigger><DialogContent className="max-w-xl"><DialogHeader><DialogTitle>添加来源连接器</DialogTitle><DialogDescription>选择现有提供方并填写其原生配置。密钥应单独保存在加密凭据库。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-1.5"><Label>连接器类型</Label><Select value={connectorType} onValueChange={(value) => form.setValue("type", value as z.infer<typeof connectionSchema>["type"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["local-directory", "git", "url", "zotero", "notion", "confluence", "sharepoint", "s3"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor="connection-name">显示名称</Label><Input id="connection-name" {...form.register("name")} /></div><div className="space-y-1.5 sm:col-span-2"><Label htmlFor="connection-config">连接器配置（JSON）</Label><Textarea id="connection-config" className="min-h-40 font-mono text-xs" spellCheck={false} {...form.register("config")} /><p className="text-[11px] text-muted-foreground">字段严格沿用后端连接器配置，不在浏览器中改写路径或 URL。</p></div><div className="space-y-1.5"><Label>凭据（可选）</Label><Select value={credentialId || "none"} onValueChange={(value) => form.setValue("credentialId", value === "none" ? "" : value)}><SelectTrigger><SelectValue placeholder="不使用凭据" /></SelectTrigger><SelectContent><SelectItem value="none">不使用凭据</SelectItem>{credentials.map((credential) => { const id = textValue(credential.credential_id, ""); return <SelectItem key={id} value={id}>{textValue(credential.label, id)} · {textValue(credential.provider)}</SelectItem>; })}</SelectContent></Select>{!credentials.length ? <p className="text-[11px] text-muted-foreground">需要鉴权的连接器请先通过页面右上角添加凭据。</p> : null}</div><label className="flex items-center gap-2 self-end pb-2 text-[13px]"><input type="checkbox" className="size-4 accent-primary" {...form.register("visible")} />工作区可见</label>{form.formState.errors.root ? <p className="text-xs text-error sm:col-span-2">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>保存连接器</Button></DialogFooter></form></DialogContent></Dialog>;
}

function CredentialDialog({ kbId }: { kbId: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof credentialSchema>>({ resolver: zodResolver(credentialSchema), defaultValues: { provider: "notion", label: "", secrets: "{\n  \"token\": \"\"\n}" } });
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof credentialSchema>) => controlApi.createConnectorCredential(kbId, { provider: values.provider, credential_kind: "static", label: values.label, secret_values: parseObject(values.secrets), scopes: [] }), onSuccess: async () => { toast.success("加密凭据已保存"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => form.setError("root", { message: error.message }) });
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
    onSuccess: async () => { toast.success("凭据已原子轮换"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["sources"] }); },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="轮换凭据"><KeyRound className="size-4" /></Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>轮换加密凭据</DialogTitle><DialogDescription>填写完整的新密钥字段。明文不会在响应或页面状态中回显。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-1.5"><Label htmlFor={`rotate-${id}`}>新密钥字段（JSON）</Label><Textarea id={`rotate-${id}`} className="min-h-32 font-mono text-xs" {...form.register("secrets")} />{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>确认轮换</Button></DialogFooter></form></DialogContent></Dialog>;
}

function SourceVersionTools({ kbId, sourceId, versions }: { kbId: string; sourceId: string; versions: JsonRecord[] }) {
  const queryClient = useQueryClient();
  const currentId = textValue(versions.find((row) => Boolean(row.is_current))?.version_id, textValue(versions[0]?.version_id, ""));
  const historical = versions.filter((row) => !Boolean(row.is_current) && Boolean(row.artifact_available));
  const [fromId, setFromId] = useState(textValue(versions.find((row) => textValue(row.version_id, "") !== currentId)?.version_id, ""));
  const [toId, setToId] = useState(currentId);
  const [deleteId, setDeleteId] = useState(textValue(historical[0]?.version_id, ""));
  const [confirmed, setConfirmed] = useState(false);
  const [recovery, setRecovery] = useState<{ token: string; versionId: string } | null>(null);
  const diff = useMutation({
    mutationFn: () => controlApi.sourceVersionDiff(kbId, sourceId, fromId, toId),
    onError: (error) => toast.error(error.message),
  });
  const removeArtifact = useMutation({
    mutationFn: () => controlApi.deleteSourceArtifact(kbId, sourceId, deleteId),
    onSuccess: async (value) => {
      const row = isRecord(value) ? value : {};
      setRecovery({ token: textValue(row.recovery_token, ""), versionId: deleteId });
      setConfirmed(false);
      toast.success("历史原件已移入可恢复区");
      await queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const restoreArtifact = useMutation({
    mutationFn: () => {
      if (!recovery?.token) throw new Error("没有可用的恢复令牌");
      return controlApi.restoreSourceArtifact(kbId, recovery.token);
    },
    onSuccess: async () => {
      toast.success("来源原件已恢复");
      setRecovery(null);
      await queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (error) => toast.error(error.message),
  });
  const diffRow = isRecord(diff.data) ? diff.data : {};
  if (versions.length < 2 && !historical.length && !recovery) return null;
  return <div className="border-b border-border bg-surface-subtle p-3">
    {versions.length >= 2 ? <div className="space-y-2"><div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-2"><Select value={fromId} onValueChange={setFromId}><SelectTrigger aria-label="对比基线"><SelectValue placeholder="对比基线" /></SelectTrigger><SelectContent>{versions.map((row) => <SelectItem key={textValue(row.version_id)} value={textValue(row.version_id)}>{Boolean(row.is_current) ? "当前 · " : "历史 · "}{textValue(row.version_id)}</SelectItem>)}</SelectContent></Select><Select value={toId} onValueChange={setToId}><SelectTrigger aria-label="目标版本"><SelectValue placeholder="目标版本" /></SelectTrigger><SelectContent>{versions.map((row) => <SelectItem key={textValue(row.version_id)} value={textValue(row.version_id)}>{Boolean(row.is_current) ? "当前 · " : "历史 · "}{textValue(row.version_id)}</SelectItem>)}</SelectContent></Select><Button size="compact" disabled={!fromId || !toId || fromId === toId} loading={diff.isPending} onClick={() => diff.mutate()}>比较</Button></div>{diff.data ? <div className="border border-border bg-surface p-3"><div className="mb-2 flex items-center gap-2 text-[11px] text-muted-foreground"><Badge>{textValue(diffRow.kind, "unknown")}</Badge><span className="text-success">+{numberValue(diffRow.added_lines)}</span><span className="text-error">-{numberValue(diffRow.removed_lines)}</span>{Boolean(diffRow.truncated) ? <span>已截断</span> : null}</div>{textValue(diffRow.diff, "") ? <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5">{textValue(diffRow.diff, "")}</pre> : <p className="text-xs text-muted-foreground">二进制版本已核对摘要，没有可展示的逐行差异。</p>}</div> : null}</div> : null}
    {(historical.length || recovery) ? <details className="mt-3 border-t border-border pt-3"><summary className="cursor-pointer text-xs font-medium">原件保留与恢复</summary><div className="mt-3 space-y-3">{historical.length ? <><Select value={deleteId} onValueChange={(value) => { setDeleteId(value); setConfirmed(false); }}><SelectTrigger aria-label="可删除历史原件"><SelectValue /></SelectTrigger><SelectContent>{historical.map((row) => <SelectItem key={textValue(row.version_id)} value={textValue(row.version_id)}>{textValue(row.version_id)}</SelectItem>)}</SelectContent></Select><label className="flex items-start gap-2 text-xs text-muted-foreground"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} className="mt-0.5 size-4 accent-primary" />我确认只移除历史原件；版本元数据与当前在线版本不受影响。</label><Button variant="destructive" size="compact" disabled={!confirmed || !deleteId} loading={removeArtifact.isPending} onClick={() => removeArtifact.mutate()}><Trash2 className="size-3.5" />移入可恢复区</Button></> : <p className="text-xs text-muted-foreground">没有可移除的历史原件；当前在线版本始终受保护。</p>}{recovery?.token ? <div className="flex items-center justify-between gap-3 border-l-2 border-warning bg-warning-subtle px-3 py-2"><p className="text-xs">待恢复版本：<span className="font-mono">{recovery.versionId}</span></p><Button size="compact" loading={restoreArtifact.isPending} onClick={() => restoreArtifact.mutate()}><RotateCcw className="size-3.5" />恢复原件</Button></div> : null}</div></details> : null}
  </div>;
}

function IntegrationsWorkspace() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const knowledgeBases = useKnowledgeBases();
  const selectedKbId = useWorkspaceStore((state) => state.selectedKnowledgeBaseId);
  const setSelectedKbId = useWorkspaceStore((state) => state.setSelectedKnowledgeBaseId);
  const requestedKbId = searchParams.get("kb")?.trim() || "";
  const availableKbIds = useMemo(() => new Set((knowledgeBases.data ?? []).map((kb) => kb.kb_id)), [knowledgeBases.data]);
  const kbId = (requestedKbId && (knowledgeBases.isPending || availableKbIds.has(requestedKbId)) ? requestedKbId : "")
    || (selectedKbId && availableKbIds.has(selectedKbId) ? selectedKbId : "")
    || knowledgeBases.data?.[0]?.kb_id
    || "";
  const requestedTab = searchParams.get("tab");
  const activeTab = integrationTabs.includes(requestedTab as (typeof integrationTabs)[number]) ? requestedTab! : "connections";
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const connections = useQuery({ queryKey: ["sources", "connections", kbId], queryFn: () => controlApi.connections(kbId), enabled: Boolean(kbId) });
  const health = useQuery({ queryKey: ["sources", "health", kbId], queryFn: () => controlApi.connectionHealth(kbId), enabled: Boolean(kbId), refetchInterval: 10_000 });
  const jobs = useQuery({ queryKey: ["sources", "jobs", kbId], queryFn: () => controlApi.syncJobs(kbId), enabled: Boolean(kbId), refetchInterval: 5000 });
  const credentials = useQuery({ queryKey: ["sources", "credentials", kbId], queryFn: () => controlApi.connectorCredentials(kbId), enabled: Boolean(kbId), retry: false });
  const credentialEvents = useQuery({ queryKey: ["sources", "credential-events", kbId], queryFn: () => controlApi.connectorCredentialEvents(kbId), enabled: Boolean(kbId), retry: false });
  const catalog = useQuery({ queryKey: ["sources", "catalog", kbId], queryFn: () => controlApi.sourceCatalog(kbId, true), enabled: Boolean(kbId) });
  const artifactUsage = useQuery({ queryKey: ["sources", "artifact-usage", kbId], queryFn: () => controlApi.sourceArtifactUsage(kbId), enabled: Boolean(kbId), retry: false });
  const connectionRows = records(connections.data, ["connections", "items"]);
  const healthRows = records(health.data, ["connections", "items"]).map((row) => ({ ...row, status: row.status ?? row.health_status }) as JsonRecord);
  const jobRows = records(jobs.data, ["jobs", "items"]);
  const credentialRows = records(credentials.data, ["credentials", "items"]);
  const credentialEventRows = records(credentialEvents.data, ["events", "items"]);
  const sourceRows = records(catalog.data, ["sources", "items", "entries"]).map((row) => ({ ...row, title: row.title ?? row.display_name }) as JsonRecord);
  const activeSourceId = selectedSourceId || textValue(sourceRows[0]?.source_id, "");
  const activeSource = sourceRows.find((row) => textValue(row.source_id, "") === activeSourceId);
  const versions = useQuery({ queryKey: ["sources", "versions", kbId, activeSourceId], queryFn: () => controlApi.sourceVersions(kbId, activeSourceId), enabled: Boolean(kbId && activeSourceId) });
  const versionRows = records(versions.data, ["versions", "items"]);
  const usageRow = isRecord(artifactUsage.data) ? artifactUsage.data : {};
  const healthByConnection = useMemo(() => new Map(healthRows.map((row) => [textValue(row.connection_id, ""), row])), [healthRows]);
  const sync = useMutation({ mutationFn: (id: string) => controlApi.syncConnection(kbId, id), onSuccess: async () => { toast.success("同步任务已启动"); await queryClient.invalidateQueries({ queryKey: ["sources", "jobs"] }); }, onError: (error) => toast.error(error.message) });
  const toggle = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => controlApi.updateConnection(kbId, id, { enabled }), onSuccess: async () => { toast.success("连接器状态已更新"); await queryClient.invalidateQueries({ queryKey: ["sources", "connections"] }); }, onError: (error) => toast.error(error.message) });
  const replay = useMutation({ mutationFn: (id: string) => controlApi.replaySyncJob(kbId, id), onSuccess: async () => { toast.success("失败任务已重放"); await queryClient.invalidateQueries({ queryKey: ["sources", "jobs"] }); }, onError: (error) => toast.error(error.message) });
  const deleteConnection = useMutation({ mutationFn: (id: string) => controlApi.deleteConnection(kbId, id), onSuccess: async () => { toast.success("连接器已删除"); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => toast.error(error.message) });
  const refreshCredential = useMutation({ mutationFn: (row: JsonRecord) => controlApi.refreshConnectorCredential(kbId, textValue(row.credential_id, ""), numberValue(row.revision, 1)), onSuccess: async () => { toast.success("OAuth 凭据已刷新"); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => toast.error(error.message) });
  const deleteCredential = useMutation({ mutationFn: (row: JsonRecord) => controlApi.deleteConnectorCredential(kbId, textValue(row.credential_id, ""), numberValue(row.revision, 1)), onSuccess: async () => { toast.success("未绑定凭据已删除"); await queryClient.invalidateQueries({ queryKey: ["sources"] }); }, onError: (error) => toast.error(error.message) });
  const downloadVersion = useMutation({ mutationFn: async ({ sourceId, versionId, name }: { sourceId: string; versionId: string; name: string }) => ({ name, blob: await apiDownload(`/knowledge-bases/${encodeURIComponent(kbId)}/source-catalog/${encodeURIComponent(sourceId)}/versions/${encodeURIComponent(versionId)}/content`) }), onSuccess: ({ name, blob }) => { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url); toast.success("来源原件已下载"); }, onError: (error) => toast.error(error.message) });
  const refreshAll = () => void queryClient.invalidateQueries({ queryKey: ["sources"] });
  useEffect(() => {
    if (kbId && kbId !== selectedKbId) setSelectedKbId(kbId);
  }, [kbId, selectedKbId, setSelectedKbId]);
  const updateLocation = (nextKbId: string, nextTab = activeTab) => {
    const query = new URLSearchParams(searchParams.toString());
    query.set("kb", nextKbId);
    if (nextTab === "connections") query.delete("tab");
    else query.set("tab", nextTab);
    router.replace(`${pathname}?${query.toString()}`);
  };
  if (!knowledgeBases.isPending && !kbId) {
    return <div className="min-h-full"><PageHeader eyebrow="External data" title="数据接入" description="连接外部系统，并将内容同步为 CogDoc 可检索的知识库文档。" /><div className="p-4 md:p-6"><EmptyState icon={Cable} title="还没有可接入的知识库" description="先创建知识库，再配置连接器、凭据和同步任务。" action={<Button asChild variant="primary"><Link href="/knowledge">创建知识库</Link></Button>} /></div></div>;
  }
  return <div className="min-h-full"><PageHeader eyebrow="External data" title="数据接入" description="连接外部系统，并将内容同步为 CogDoc 可检索的知识库文档。" meta={kbId ? <Badge>{kbId}</Badge> : undefined} actions={kbId ? <><OAuthDialog kbId={kbId} connections={connectionRows} /><CredentialDialog kbId={kbId} /><ConnectionDialog kbId={kbId} credentials={credentialRows} /></> : undefined} /><div className="p-4 md:p-6">
    <div className="mb-4 flex flex-col gap-3 border border-border bg-surface px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex min-w-0 items-center gap-3"><label htmlFor="integration-kb" className="shrink-0 text-xs font-medium">目标知识库</label><select id="integration-kb" value={kbId} disabled={knowledgeBases.isPending} onChange={(event) => { setSelectedSourceId(""); setSelectedKbId(event.target.value); updateLocation(event.target.value); }} className="h-8 min-w-52 rounded-[6px] border border-border bg-surface px-2.5 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary">{(knowledgeBases.data ?? []).map((kb) => <option key={kb.kb_id} value={kb.kb_id}>{kb.kb_id} · {kb.document_count} 文档</option>)}</select></div>
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground"><span>外部系统</span><ArrowRight className="size-3" /><span>连接与同步</span><ArrowRight className="size-3" /><Link href={`/knowledge/${encodeURIComponent(kbId)}`} className="font-medium text-foreground hover:text-primary">知识库文档</Link></div>
    </div>
    <Tabs value={activeTab} onValueChange={(value) => updateLocation(kbId, value)}><div className="mb-4 flex items-end justify-between"><TabsList><TabsTrigger value="connections">连接器 <Badge className="ml-1">{connectionRows.length}</Badge></TabsTrigger><TabsTrigger value="catalog">来源目录 <Badge className="ml-1">{sourceRows.length}</Badge></TabsTrigger><TabsTrigger value="jobs">同步任务 <Badge className="ml-1">{jobRows.length}</Badge></TabsTrigger><TabsTrigger value="credentials">凭据 <Badge className="ml-1">{credentialRows.length}</Badge></TabsTrigger><TabsTrigger value="credential-audit">凭据审计</TabsTrigger></TabsList><Button variant="ghost" size="icon" onClick={refreshAll} aria-label="刷新数据接入"><RefreshCw className={cn("size-4", connections.isFetching && "animate-spin")} /></Button></div>
    <TabsContent value="connections"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={connections.isPending} error={connections.error} onRetry={() => void connections.refetch()} />{connections.data && !connectionRows.length ? <EmptyState icon={Cable} compact title="没有来源连接器" description="添加本地目录、Git、URL、Notion、Confluence、SharePoint、Zotero 或 S3。" action={<ConnectionDialog kbId={kbId} credentials={credentialRows} />} /> : <div className="divide-y divide-border">{connectionRows.map((row) => { const id = textValue(row.connection_id, ""); const h = healthByConnection.get(id); const enabled = Boolean(row.enabled); const name = textValue(row.name, id); return <div key={id} className="grid grid-cols-[minmax(0,1fr)_130px_120px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div className="min-w-0"><div className="flex items-center gap-2"><p className="truncate font-medium">{name}</p><Badge>{textValue(row.connector_type)}</Badge></div><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{id}</p></div><StatusBadge status={textValue(h?.status, enabled ? "ready" : "disabled")} /><span className="text-xs text-muted-foreground">{textValue(row.credential_source, "无凭据")}</span><div className="flex gap-1"><Button variant="ghost" size="icon" disabled={!enabled} loading={sync.isPending && sync.variables === id} onClick={() => sync.mutate(id)} aria-label="立即同步"><Play className="size-4" /></Button><Button variant="ghost" size="icon" onClick={() => toggle.mutate({ id, enabled: !enabled })} aria-label={enabled ? "停用连接器" : "启用连接器"}><Unplug className="size-4" /></Button><ConfirmDeleteButton title="删除连接器" description={`将删除“${name}”的配置；已有同步审计与来源版本仍按后端保留策略处理。`} loading={deleteConnection.isPending && deleteConnection.variables === id} onConfirm={() => deleteConnection.mutate(id)} /></div></div>; })}</div>}</section></TabsContent>
    <TabsContent value="catalog"><div className="mb-3 grid gap-px overflow-hidden border border-border bg-border sm:grid-cols-4">{[["活动版本", numberValue(usageRow.active_versions)], ["活动原件", numberValue(usageRow.active_bytes)], ["恢复区版本", numberValue(usageRow.trash_versions)], ["恢复区原件", numberValue(usageRow.trash_bytes)]].map(([label, value], index) => <div key={String(label)} className="bg-surface px-3 py-2"><p className="text-[10px] text-muted-foreground">{label}</p><p className="mt-0.5 font-mono text-xs">{index % 2 ? `${Number(value).toLocaleString()} bytes` : value}</p></div>)}</div><div className="grid gap-4 xl:grid-cols-[minmax(360px,0.9fr)_minmax(420px,1.1fr)]"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={catalog.isPending} error={catalog.error} onRetry={() => void catalog.refetch()} />{catalog.data && !sourceRows.length ? <EmptyState icon={Route} compact title="来源目录为空" description="连接器完成同步后，会生成可追踪的来源和版本记录。" /> : <div className="divide-y divide-border">{sourceRows.map((row) => { const id = textValue(row.source_id, ""); return <button key={id} onClick={() => setSelectedSourceId(id)} className={cn("w-full px-4 py-3 text-left hover:bg-surface-subtle", activeSourceId === id && "bg-primary-subtle")}><div className="flex items-center justify-between gap-2"><p className="truncate text-[13px] font-medium">{textValue(row.title, textValue(row.name, id))}</p><StatusBadge status={textValue(row.health_status, textValue(row.status, "ready"))} /></div><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{textValue(row.origin_uri, id)}</p></button>; })}</div>}</section><section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">版本航迹</h3><p className="text-[11px] text-muted-foreground">{activeSourceId || "选择来源"}</p></div><QueryState pending={versions.isPending} error={versions.error} onRetry={() => void versions.refetch()} />{versions.data && !versionRows.length ? <EmptyState icon={DatabaseZap} compact title="没有版本记录" description="每次来源内容变化都会形成不可变版本。" /> : <><SourceVersionTools key={activeSourceId} kbId={kbId} sourceId={activeSourceId} versions={versionRows} /><div className="divide-y divide-border">{versionRows.map((row) => { const versionId = textValue(row.version_id); const available = Boolean(row.artifact_available ?? true); return <div key={versionId} className="px-4 py-3"><div className="flex items-center justify-between gap-2"><p className="font-mono text-[11px] font-medium">{versionId}</p><div className="flex items-center gap-1"><StatusBadge status={textValue(row.artifact_status, available ? "ready" : "metadata_only")} />{available ? <Button variant="ghost" size="icon" onClick={() => downloadVersion.mutate({ sourceId: activeSourceId, versionId, name: textValue(activeSource?.display_name, textValue(activeSource?.name, `${activeSourceId}-${versionId}`)) })} aria-label="下载来源版本"><Download className="size-4" /></Button> : null}</div></div><p className="mt-1 text-[11px] text-muted-foreground">{textValue(row.created_at, textValue(row.fetched_at))} · {textValue(row.byte_size, textValue(row.size_bytes, "未知大小"))} bytes</p></div>; })}</div></>}</section></div></TabsContent>
    <TabsContent value="jobs"><section className="overflow-hidden border border-border bg-surface">{jobRows.length ? <div className="divide-y divide-border">{jobRows.map((row) => { const id = textValue(row.job_id); const status = textValue(row.status, "pending"); const errorMessage = textValue(row.error_message, ""); return <div key={id} className="grid grid-cols-[minmax(0,1fr)_110px_180px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div className="min-w-0"><p className="font-medium">{textValue(row.connector_type)} 同步</p><p className="font-mono text-[10px] text-muted-foreground">{id}</p>{errorMessage ? <p className="mt-1 truncate text-[11px] text-error" title={errorMessage}>{errorMessage === "provider HTTP 401" ? "提供方认证失败（HTTP 401），请检查或轮换凭据。" : errorMessage}</p> : null}</div><StatusBadge status={status} /><span className="text-xs text-muted-foreground">{textValue(row.documents_fetched, "0")} 文档 · 尝试 {textValue(row.attempt, "0")}{row.finished_at ? ` · ${formatDateTime(row.finished_at as string | number)}` : ""}</span>{status === "dead_letter" ? <Button size="compact" onClick={() => replay.mutate(id)} loading={replay.isPending && replay.variables === id}><RotateCcw className="size-3.5" />重放</Button> : <span />}</div>; })}</div> : <EmptyState icon={RefreshCw} compact title="没有同步任务" description="从连接器行启动一次同步。" />}</section></TabsContent>
    <TabsContent value="credentials"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={credentials.isPending} error={credentials.error} onRetry={() => void credentials.refetch()} />{Boolean(credentials.data) && (credentialRows.length ? <div className="divide-y divide-border">{credentialRows.map((row) => { const bound = Boolean(textValue(row.connection_id, "")); const oauth = textValue(row.credential_kind, "static") === "oauth"; const id = textValue(row.credential_id); const label = textValue(row.label, id); return <div key={id} className="grid grid-cols-[minmax(0,1fr)_100px_180px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div><p className="font-medium">{label}</p><p className="font-mono text-[10px] text-muted-foreground">{id}</p></div><Badge>{textValue(row.provider)}</Badge><span className="text-xs text-muted-foreground">字段：{Array.isArray(row.secret_fields) ? row.secret_fields.join(", ") : "已加密"}{bound ? " · 已绑定" : ""}</span><div className="flex gap-1">{oauth ? <Button variant="ghost" size="icon" onClick={() => refreshCredential.mutate(row)} aria-label="刷新 OAuth 凭据"><RefreshCw className="size-4" /></Button> : <RotateCredentialDialog kbId={kbId} credential={row} />}<ConfirmDeleteButton title="删除凭据" description={`将永久删除“${label}”的加密密钥。已绑定连接器的凭据必须先解除绑定。`} disabled={bound} loading={deleteCredential.isPending && deleteCredential.variables === row} onConfirm={() => deleteCredential.mutate(row)} /></div></div>; })}</div> : <EmptyState icon={KeyRound} compact title="没有凭据" description="静态密钥会加密保存；OAuth 凭据由授权流程创建。" action={<CredentialDialog kbId={kbId} />} />)}</section></TabsContent>
    <TabsContent value="credential-audit"><section className="overflow-hidden border border-border bg-surface"><QueryState pending={credentialEvents.isPending} error={credentialEvents.error} onRetry={() => void credentialEvents.refetch()} />{credentialEvents.data && !credentialEventRows.length ? <EmptyState icon={KeyRound} compact title="没有凭据操作记录" description="创建、轮换、刷新和删除凭据后会留下审计事件。" /> : <div className="divide-y divide-border">{credentialEventRows.map((row, index) => <div key={textValue(row.event_id, String(index))} className="grid grid-cols-[150px_140px_minmax(0,1fr)_160px] gap-3 px-4 py-3 text-[12px]"><span className="font-medium">{textValue(row.action, "unknown")}</span><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.credential_id)}</span><span className="font-mono text-[10px] text-muted-foreground">actor {textValue(row.actor_id)}</span><span className="font-mono text-[10px] text-muted-foreground">{formatDateTime(row.occurred_at as string | number | null | undefined)}</span></div>)}</div>}</section></TabsContent>
  </Tabs></div></div>;
}

export default function IntegrationsPage() {
  return <Suspense fallback={<div className="flex min-h-[320px] items-center justify-center gap-2 text-sm text-muted-foreground"><Spinner />正在打开数据接入</div>}><IntegrationsWorkspace /></Suspense>;
}
