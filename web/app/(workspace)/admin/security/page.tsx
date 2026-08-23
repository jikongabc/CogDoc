"use client";

import { useEffect } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { KeyRound, LogOut, Shield, Smartphone } from "lucide-react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { AdminPageFrame } from "@/components/admin/admin-nav";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";

const passwordSchema = z.object({ current: z.string().min(1), next: z.string().min(12, "新密码至少 12 个字符").max(256), confirm: z.string() }).refine((value) => value.next === value.confirm, { path: ["confirm"], message: "两次输入的新密码不一致" });
const policySchema = z.object({ idle: z.number().int().min(1).max(43200), absolute: z.number().int().min(1).max(8760), maxSessions: z.number().int().min(1).max(100) });

export default function SecurityPage() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const clearSession = useSessionStore((state) => state.clearSession);
  const workspaceId = useSessionStore((state) => state.workspace?.workspace_id || "");
  const authMode = useSessionStore((state) => state.authMode);
  const policy = useQuery({ queryKey: ["admin", "session-policy", workspaceId], queryFn: () => controlApi.sessionPolicy(workspaceId), enabled: Boolean(workspaceId) });
  const sessions = useQuery({ queryKey: ["admin", "security-sessions", workspaceId], queryFn: () => controlApi.securitySessions(workspaceId, true), enabled: Boolean(workspaceId) });
  const policyRow = isRecord(policy.data) ? policy.data : {};
  const policyForm = useForm<z.infer<typeof policySchema>>({ resolver: zodResolver(policySchema), defaultValues: { idle: 60, absolute: 24, maxSessions: 10 } });
  const passwordForm = useForm<z.infer<typeof passwordSchema>>({ resolver: zodResolver(passwordSchema), defaultValues: { current: "", next: "", confirm: "" } });
  useEffect(() => {
    if (!policy.data) return;
    policyForm.reset({ idle: numberValue(policyRow.idle_timeout_minutes, 60), absolute: numberValue(policyRow.absolute_timeout_hours, 24), maxSessions: numberValue(policyRow.max_active_sessions, 10) });
  }, [policy.data, policyForm, policyRow.absolute_timeout_hours, policyRow.idle_timeout_minutes, policyRow.max_active_sessions]);
  const savePolicy = useMutation({ mutationFn: (values: z.infer<typeof policySchema>) => controlApi.updateSessionPolicy(workspaceId, { idle_timeout_minutes: values.idle, absolute_timeout_hours: values.absolute, max_active_sessions: values.maxSessions, expected_revision: numberValue(policyRow.revision) }), onSuccess: async () => { toast.success("会话策略已保存"); await queryClient.invalidateQueries({ queryKey: ["admin", "session-policy"] }); }, onError: (error) => toast.error(error.message) });
  const changePassword = useMutation({ mutationFn: (values: z.infer<typeof passwordSchema>) => controlApi.changePassword(values.current, values.next), onSuccess: () => { toast.success("密码已更新"); passwordForm.reset(); }, onError: (error) => passwordForm.setError("root", { message: error.message }) });
  const revoke = useMutation({ mutationFn: (row: JsonRecord) => controlApi.revokeSecuritySession(workspaceId, textValue(row.session_id, textValue(row.id, ""))), onSuccess: async () => { toast.success("会话已撤销"); await queryClient.invalidateQueries({ queryKey: ["admin", "security-sessions"] }); }, onError: (error) => toast.error(error.message) });
  const logoutAll = useMutation({ mutationFn: controlApi.logoutAll, onSuccess: () => { clearSession(); router.replace("/login"); }, onError: (error) => toast.error(error.message) });
  const sessionRows = records(sessions.data, ["items", "sessions"]);
  return <AdminPageFrame><PageHeader eyebrow="Session controls" title="会话安全" description="管理密码、工作区会话策略和已登录设备。" actions={authMode === "account" ? <Button variant="secondary" onClick={() => logoutAll.mutate()} loading={logoutAll.isPending}><LogOut className="size-4" />退出所有设备</Button> : undefined} />{!workspaceId ? <EmptyState icon={Shield} title="本地模式没有账号会话" description="启用账号认证后，可在这里强制空闲超时、绝对超时和并发会话上限。" /> : <div className="grid gap-6 p-4 md:p-6 xl:grid-cols-2">
    <section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">工作区会话策略</h3><p className="mt-0.5 text-[11px] text-muted-foreground">策略变更由后端修订号保护。</p></div><form className="grid gap-4 p-4 sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3" onSubmit={policyForm.handleSubmit((values) => savePolicy.mutate(values))}><div className="space-y-1.5"><Label htmlFor="idle">空闲超时（分钟）</Label><Input id="idle" type="number" {...policyForm.register("idle", { valueAsNumber: true })} /></div><div className="space-y-1.5"><Label htmlFor="absolute">最长会话（小时）</Label><Input id="absolute" type="number" {...policyForm.register("absolute", { valueAsNumber: true })} /></div><div className="space-y-1.5"><Label htmlFor="max-sessions">并发会话上限</Label><Input id="max-sessions" type="number" {...policyForm.register("maxSessions", { valueAsNumber: true })} /></div><div className="sm:col-span-3 xl:col-span-1 2xl:col-span-3"><Button type="submit" variant="primary" loading={savePolicy.isPending}>保存会话策略</Button></div></form></section>
    <section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">修改密码</h3><p className="mt-0.5 text-[11px] text-muted-foreground">修改后按组织策略处理现有会话。</p></div><form className="space-y-3 p-4" onSubmit={passwordForm.handleSubmit((values) => changePassword.mutate(values))}><div className="space-y-1.5"><Label htmlFor="current-password">当前密码</Label><Input id="current-password" type="password" autoComplete="current-password" {...passwordForm.register("current")} /></div><div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><Label htmlFor="new-password">新密码</Label><Input id="new-password" type="password" autoComplete="new-password" {...passwordForm.register("next")} /></div><div className="space-y-1.5"><Label htmlFor="confirm-password">确认新密码</Label><Input id="confirm-password" type="password" autoComplete="new-password" {...passwordForm.register("confirm")} /></div></div>{passwordForm.formState.errors.confirm ? <p className="text-xs text-error">{passwordForm.formState.errors.confirm.message}</p> : null}{passwordForm.formState.errors.root ? <p className="text-xs text-error">{passwordForm.formState.errors.root.message}</p> : null}<Button type="submit" loading={changePassword.isPending}><KeyRound className="size-4" />更新密码</Button></form></section>
    <section className="border border-border bg-surface xl:col-span-2"><div className="flex h-11 items-center justify-between border-b border-border px-4"><div><h3 className="text-[13px] font-semibold">工作区会话</h3><p className="text-[11px] text-muted-foreground">活动和已撤销的设备会话</p></div></div><QueryState pending={sessions.isPending} error={sessions.error} onRetry={() => void sessions.refetch()} />{sessions.data && !sessionRows.length ? <EmptyState icon={Smartphone} compact title="没有会话记录" description="成员登录后，设备与会话状态会显示在这里。" /> : <div className="divide-y divide-border">{sessionRows.map((row) => <div key={textValue(row.session_id, textValue(row.id))} className="grid grid-cols-[minmax(0,1fr)_160px_100px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.display_name, textValue(row.email, textValue(row.user_id)))}</p><p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{textValue(row.session_id, textValue(row.id))}</p></div><span className="font-mono text-[10px] text-muted-foreground">{textValue(row.last_seen_at, textValue(row.created_at))}</span><StatusBadge status={Boolean(row.active ?? true) ? "active" : "revoked"} /><Button variant="ghost" size="compact" disabled={!Boolean(row.active ?? true)} onClick={() => revoke.mutate(row)}>撤销</Button></div>)}</div>}</section>
  </div>}</AdminPageFrame>;
}
