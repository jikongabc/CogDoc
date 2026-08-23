"use client";

import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, KeyRound, Plus, Trash2, Workflow } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { AdminPageFrame } from "@/components/admin/admin-nav";
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
import { Textarea } from "@/components/ui/textarea";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";
import { cn } from "@/lib/utils";

const accountSchema = z.object({ name: z.string().trim().min(2).max(120), description: z.string().trim().max(500), role: z.enum(["viewer", "reviewer", "editor", "admin"]) });
const tokenSchema = z.object({ label: z.string().trim().min(2).max(120), expires: z.number().int().min(1).max(3650) });

function CreateAccountDialog({ workspaceId }: { workspaceId: string }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof accountSchema>>({ resolver: zodResolver(accountSchema), defaultValues: { name: "", description: "", role: "viewer" } });
  const role = useWatch({ control: form.control, name: "role" });
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof accountSchema>) => controlApi.createServiceAccount(workspaceId, values), onSuccess: async () => { toast.success("服务账号已创建"); setOpen(false); form.reset(); await queryClient.invalidateQueries({ queryKey: ["admin", "service-accounts"] }); }, onError: (error) => form.setError("root", { message: error.message }) });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />创建服务账号</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>创建服务账号</DialogTitle><DialogDescription>为自动化和连接器创建最小权限机器身份。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor="sa-name">名称</Label><Input id="sa-name" {...form.register("name")} /></div><div className="space-y-1.5"><Label htmlFor="sa-description">说明</Label><Textarea id="sa-description" {...form.register("description")} /></div><div className="space-y-1.5"><Label>角色</Label><Select value={role} onValueChange={(value) => form.setValue("role", value as z.infer<typeof accountSchema>["role"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["viewer", "reviewer", "editor", "admin"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>创建账号</Button></DialogFooter></form></DialogContent></Dialog>;
}

function TokenDialog({ workspaceId, accountId, onSecret }: { workspaceId: string; accountId: string; onSecret: (secret: string) => void }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<z.infer<typeof tokenSchema>>({ resolver: zodResolver(tokenSchema), defaultValues: { label: "", expires: 90 } });
  const mutation = useMutation({ mutationFn: (values: z.infer<typeof tokenSchema>) => controlApi.createServiceToken(workspaceId, accountId, { label: values.label, expires_in_days: values.expires }), onSuccess: async (value) => { const row = isRecord(value) ? value : {}; const secret = textValue(row.token, textValue(row.secret, textValue(row.access_token, ""))); setOpen(false); form.reset(); onSecret(secret); await queryClient.invalidateQueries({ queryKey: ["admin", "service-tokens"] }); }, onError: (error) => form.setError("root", { message: error.message }) });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><KeyRound className="size-4" />生成令牌</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>生成 API Token</DialogTitle><DialogDescription>明文令牌只显示一次。关闭后无法再次查看。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor="token-label">用途标签</Label><Input id="token-label" {...form.register("label")} /></div><div className="space-y-1.5"><Label htmlFor="token-expiry">有效天数</Label><Input id="token-expiry" type="number" {...form.register("expires", { valueAsNumber: true })} /></div>{form.formState.errors.root ? <p className="text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>生成令牌</Button></DialogFooter></form></DialogContent></Dialog>;
}

export default function ServiceAccountsPage() {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.workspace?.workspace_id || "");
  const [selectedId, setSelectedId] = useState("");
  const [secret, setSecret] = useState("");
  const accounts = useQuery({ queryKey: ["admin", "service-accounts", workspaceId], queryFn: () => controlApi.serviceAccounts(workspaceId), enabled: Boolean(workspaceId) });
  const policy = useQuery({ queryKey: ["admin", "service-account-policy", workspaceId], queryFn: () => controlApi.serviceAccountPolicy(workspaceId), enabled: Boolean(workspaceId), retry: false });
  const accountRows = records(accounts.data, ["items", "service_accounts", "accounts"]);
  const activeId = selectedId || textValue(accountRows[0]?.service_account_id, textValue(accountRows[0]?.id, ""));
  const active = accountRows.find((row) => textValue(row.service_account_id, textValue(row.id, "")) === activeId);
  const tokens = useQuery({ queryKey: ["admin", "service-tokens", activeId], queryFn: () => controlApi.serviceTokens(workspaceId, activeId), enabled: Boolean(workspaceId && activeId) });
  const tokenRows = records(tokens.data, ["items", "tokens"]);
  const revoke = useMutation({ mutationFn: (row: JsonRecord) => controlApi.revokeServiceToken(workspaceId, activeId, textValue(row.token_id, textValue(row.id, "")), numberValue(row.revision)), onSuccess: async () => { toast.success("令牌已撤销"); await queryClient.invalidateQueries({ queryKey: ["admin", "service-tokens"] }); }, onError: (error) => toast.error(error.message) });
  const remove = useMutation({ mutationFn: () => active ? controlApi.deleteServiceAccount(workspaceId, activeId, numberValue(active.revision)) : Promise.reject(new Error("请选择服务账号")), onSuccess: async () => { toast.success("服务账号已删除"); setSelectedId(""); await queryClient.invalidateQueries({ queryKey: ["admin", "service-accounts"] }); }, onError: (error) => toast.error(error.message) });
  const policyRow = isRecord(policy.data) ? policy.data : {};
  return <AdminPageFrame><PageHeader eyebrow="Machine identity" title="服务账号" description="为自动化创建受策略约束的机器身份，并管理一次性 API Token。" actions={workspaceId ? <CreateAccountDialog workspaceId={workspaceId} /> : undefined} />{!workspaceId ? <EmptyState icon={Workflow} title="账号认证未启用" description="服务账号属于企业工作区身份体系，本地兼容模式不会创建机器账号。" /> : <div className="grid min-h-[calc(100dvh-132px)] lg:grid-cols-[310px_minmax(0,1fr)]"><section className="border-r border-border bg-surface"><div className="border-b border-border px-4 py-3"><div className="flex items-center justify-between"><h3 className="text-[13px] font-semibold">账号</h3><Badge>{accountRows.length}</Badge></div><p className="mt-1 text-[11px] text-muted-foreground">上限 {numberValue(policyRow.max_accounts, accountRows.length || 0)} · 每账号 {numberValue(policyRow.max_tokens_per_account, 0) || "按策略"} 个令牌</p></div><QueryState pending={accounts.isPending} error={accounts.error} onRetry={() => void accounts.refetch()} />{accounts.data && !accountRows.length ? <EmptyState icon={Workflow} compact title="没有服务账号" description="为 CI、连接器或内部自动化创建最小权限身份。" /> : <div className="divide-y divide-border">{accountRows.map((row) => { const id = textValue(row.service_account_id, textValue(row.id, "")); return <button key={id} onClick={() => setSelectedId(id)} className={cn("w-full px-4 py-3 text-left hover:bg-surface-subtle", activeId === id && "bg-primary-subtle")}><div className="flex items-center justify-between gap-2"><p className="truncate text-[13px] font-medium">{textValue(row.name)}</p><StatusBadge status={Boolean(row.active ?? true) ? "active" : "disabled"} /></div><p className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">{textValue(row.description, "无说明")}</p><p className="mt-2 text-[10px] uppercase tracking-wide text-muted-foreground">{textValue(row.role)}</p></button>; })}</div>}</section><section className="min-w-0 bg-surface">{active ? <><div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4"><div><h3 className="text-base font-semibold">{textValue(active.name)}</h3><p className="mt-1 font-mono text-[10px] text-muted-foreground">{activeId}</p></div><div className="flex gap-2"><TokenDialog workspaceId={workspaceId} accountId={activeId} onSecret={setSecret} /><Button variant="ghost" size="icon" className="text-error" onClick={() => remove.mutate()} aria-label="删除服务账号"><Trash2 className="size-4" /></Button></div></div>{secret ? <div className="m-5 border-l-2 border-warning bg-warning-subtle p-4"><p className="text-xs font-semibold text-warning">请立即复制令牌，关闭后无法找回</p><div className="mt-2 flex items-center gap-2"><code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-[3px] bg-surface px-3 py-2 font-mono text-xs">{secret}</code><Button size="icon" onClick={() => { void navigator.clipboard.writeText(secret); toast.success("令牌已复制"); }} aria-label="复制令牌"><Copy className="size-4" /></Button></div><Button variant="ghost" size="compact" className="mt-2" onClick={() => setSecret("")}>我已保存，隐藏令牌</Button></div> : null}<div className="p-5"><h4 className="mb-3 text-[13px] font-semibold">API Token</h4><QueryState pending={tokens.isPending} error={tokens.error} onRetry={() => void tokens.refetch()} />{tokens.data && !tokenRows.length ? <EmptyState icon={KeyRound} compact title="没有有效令牌" description="生成令牌并只在安全的密钥管理系统中保存。" /> : <div className="divide-y divide-border border border-border">{tokenRows.map((row) => <div key={textValue(row.token_id, textValue(row.id))} className="grid grid-cols-[minmax(0,1fr)_120px_100px_auto] items-center gap-3 px-3 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.label)}</p><p className="font-mono text-[10px] text-muted-foreground">{textValue(row.token_prefix, textValue(row.token_id))}</p></div><StatusBadge status={textValue(row.status, Boolean(row.active ?? true) ? "active" : "revoked")} /><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.expires_at)}</span><Button variant="ghost" size="compact" onClick={() => revoke.mutate(row)}>撤销</Button></div>)}</div>}</div></> : <EmptyState icon={Workflow} title="选择服务账号" description="查看令牌、权限和账号生命周期。" />}</section></div>}</AdminPageFrame>;
}
