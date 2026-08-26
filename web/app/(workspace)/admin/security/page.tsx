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
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAnyPermission } from "@/features/auth/permissions";
import { controlApi, isRecord, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { formatDateTime } from "@/lib/utils";
import { useSessionStore } from "@/stores/session-store";

const passwordSchema = z.object({
  current: z.string().min(1, "请输入当前密码"),
  next: z.string().min(12, "新密码至少 12 个字符").max(256),
  confirm: z.string(),
}).refine((value) => value.next === value.confirm, { path: ["confirm"], message: "两次输入的新密码不一致" });

const policySchema = z.object({
  idle: z.number().int().min(5).max(43200),
  absolute: z.number().int().min(1).max(8760),
  maxSessions: z.number().int().min(1).max(50),
});

function SectionHeader({ title, description }: { title: string; description: string }) {
  return <div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">{title}</h3><p className="mt-0.5 text-[11px] text-muted-foreground">{description}</p></div>;
}

export default function SecurityPage() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const clearSession = useSessionStore((state) => state.clearSession);
  const workspaceId = useSessionStore((state) => state.workspace?.workspace_id || "");
  const authMode = useSessionStore((state) => state.authMode);
  const canManage = useAnyPermission(["manage_access", "manage_tenant"]);
  const isAccount = authMode === "account";
  const personalSessions = useQuery({ queryKey: ["auth", "sessions"], queryFn: controlApi.authSessions, enabled: isAccount });
  const policy = useQuery({ queryKey: ["admin", "session-policy", workspaceId], queryFn: () => controlApi.sessionPolicy(workspaceId), enabled: canManage && Boolean(workspaceId) });
  const workspaceSessions = useQuery({ queryKey: ["admin", "security-sessions", workspaceId], queryFn: () => controlApi.securitySessions(workspaceId, true), enabled: canManage && Boolean(workspaceId) });
  const policyEnvelope = isRecord(policy.data) ? policy.data : {};
  const policyRow = isRecord(policyEnvelope.policy) ? policyEnvelope.policy : policyEnvelope;
  const policyForm = useForm<z.infer<typeof policySchema>>({ resolver: zodResolver(policySchema), defaultValues: { idle: 60, absolute: 24, maxSessions: 10 } });
  const passwordForm = useForm<z.infer<typeof passwordSchema>>({ resolver: zodResolver(passwordSchema), defaultValues: { current: "", next: "", confirm: "" } });

  useEffect(() => {
    if (!policy.data) return;
    policyForm.reset({
      idle: numberValue(policyRow.idle_timeout_minutes, 60),
      absolute: numberValue(policyRow.absolute_timeout_hours, 24),
      maxSessions: numberValue(policyRow.max_active_sessions, 10),
    });
  }, [policy.data, policyForm, policyRow.absolute_timeout_hours, policyRow.idle_timeout_minutes, policyRow.max_active_sessions]);

  const savePolicy = useMutation({
    mutationFn: (values: z.infer<typeof policySchema>) => controlApi.updateSessionPolicy(workspaceId, {
      idle_timeout_minutes: values.idle,
      absolute_timeout_hours: values.absolute,
      max_active_sessions: values.maxSessions,
      expected_revision: numberValue(policyRow.revision),
    }),
    onSuccess: async () => { toast.success("会话策略已保存"); await queryClient.invalidateQueries({ queryKey: ["admin", "session-policy"] }); },
    onError: (error) => toast.error(error.message),
  });
  const changePassword = useMutation({
    mutationFn: (values: z.infer<typeof passwordSchema>) => controlApi.changePassword(values.current, values.next),
    onSuccess: () => { toast.success("密码已更新"); passwordForm.reset(); },
    onError: (error) => passwordForm.setError("root", { message: error.message }),
  });
  const revokePersonal = useMutation({
    mutationFn: controlApi.revokeAuthSession,
    onSuccess: async () => { toast.success("设备会话已撤销"); await queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] }); },
    onError: (error) => toast.error(error.message),
  });
  const revokeWorkspace = useMutation({
    mutationFn: (row: JsonRecord) => controlApi.revokeSecuritySession(workspaceId, textValue(row.session_id, textValue(row.id, ""))),
    onSuccess: async () => { toast.success("成员会话已撤销"); await queryClient.invalidateQueries({ queryKey: ["admin", "security-sessions"] }); },
    onError: (error) => toast.error(error.message),
  });
  const logoutAll = useMutation({
    mutationFn: controlApi.logoutAll,
    onSuccess: () => { clearSession(); router.replace("/login"); },
    onError: (error) => toast.error(error.message),
  });
  const personalRows = personalSessions.data?.sessions ?? [];
  const workspaceRows = records(workspaceSessions.data, ["items", "sessions"]).map((row) => ({ ...row, active: textValue(row.status, "active") === "active" }) as JsonRecord);

  return <AdminPageFrame allowAccountUser>
    <PageHeader
      eyebrow="Account security"
      title="账号与会话安全"
      description={canManage ? "管理个人登录设备，以及工作区会话策略和成员会话。" : "修改密码并管理你自己的登录设备。"}
      actions={isAccount ? <Button variant="secondary" onClick={() => logoutAll.mutate()} loading={logoutAll.isPending}><LogOut className="size-4" />退出所有设备</Button> : undefined}
    />
    {!isAccount ? <EmptyState icon={Shield} title="本地模式没有账号会话" description="启用账号认证后，可在这里修改密码并撤销登录设备。" /> : <div className="grid gap-6 p-4 md:p-6 xl:grid-cols-2">
      <section className="border border-border bg-surface">
        <SectionHeader title="修改密码" description="新密码至少 12 个字符；后端继续执行组织密码策略。" />
        <form className="space-y-3 p-4" onSubmit={passwordForm.handleSubmit((values) => changePassword.mutate(values))}>
          <div className="space-y-1.5"><Label htmlFor="current-password">当前密码</Label><Input id="current-password" type="password" autoComplete="current-password" {...passwordForm.register("current")} />{passwordForm.formState.errors.current ? <p className="text-xs text-error">{passwordForm.formState.errors.current.message}</p> : null}</div>
          <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><Label htmlFor="new-password">新密码</Label><Input id="new-password" type="password" autoComplete="new-password" {...passwordForm.register("next")} /></div><div className="space-y-1.5"><Label htmlFor="confirm-password">确认新密码</Label><Input id="confirm-password" type="password" autoComplete="new-password" {...passwordForm.register("confirm")} /></div></div>
          {passwordForm.formState.errors.next ? <p className="text-xs text-error">{passwordForm.formState.errors.next.message}</p> : null}
          {passwordForm.formState.errors.confirm ? <p className="text-xs text-error">{passwordForm.formState.errors.confirm.message}</p> : null}
          {passwordForm.formState.errors.root ? <p className="text-xs text-error">{passwordForm.formState.errors.root.message}</p> : null}
          <Button type="submit" loading={changePassword.isPending}><KeyRound className="size-4" />更新密码</Button>
        </form>
      </section>
      {canManage ? <section className="border border-border bg-surface">
        <SectionHeader title="工作区会话策略" description="限制空闲时间、绝对时长和单个账号的并发会话数。" />
        <form className="grid gap-4 p-4 sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3" onSubmit={policyForm.handleSubmit((values) => savePolicy.mutate(values))}>
          <div className="space-y-1.5"><Label htmlFor="idle">空闲超时（分钟）</Label><Input id="idle" type="number" min={5} max={43200} {...policyForm.register("idle", { valueAsNumber: true })} /></div>
          <div className="space-y-1.5"><Label htmlFor="absolute">最长会话（小时）</Label><Input id="absolute" type="number" min={1} max={8760} {...policyForm.register("absolute", { valueAsNumber: true })} /></div>
          <div className="space-y-1.5"><Label htmlFor="max-sessions">并发会话上限</Label><Input id="max-sessions" type="number" min={1} max={50} {...policyForm.register("maxSessions", { valueAsNumber: true })} /></div>
          <div className="sm:col-span-3 xl:col-span-1 2xl:col-span-3"><Button type="submit" variant="primary" loading={savePolicy.isPending}>保存会话策略</Button></div>
        </form>
      </section> : null}
      <section className="border border-border bg-surface xl:col-span-2">
        <SectionHeader title="我的登录设备" description="当前设备不能在列表中单独撤销；可使用“退出所有设备”结束全部会话。" />
        <QueryState pending={personalSessions.isPending} error={personalSessions.error} onRetry={() => void personalSessions.refetch()} />
        {personalSessions.data && !personalRows.length ? <EmptyState icon={Smartphone} compact title="没有会话记录" description="重新登录后，设备会话会显示在这里。" /> : <div className="divide-y divide-border">{personalRows.map((row) => <div key={row.session_id} className="grid grid-cols-[minmax(0,1fr)_160px_100px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{row.current ? "当前设备" : "已登录设备"}</p><p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{row.session_id}</p></div><span className="text-[11px] tabular-nums text-muted-foreground">{formatDateTime(row.last_seen_at || row.created_at)}</span><StatusBadge status={row.current ? "current" : "active"} label={row.current ? "当前" : "活动"} /><Button variant="ghost" size="compact" disabled={row.current || revokePersonal.isPending} onClick={() => revokePersonal.mutate(row.session_id)}>撤销</Button></div>)}</div>}
      </section>
      {canManage ? <section className="border border-border bg-surface xl:col-span-2">
        <SectionHeader title="工作区成员会话" description="查看活动和已撤销的成员会话，并按设备强制退出。" />
        <QueryState pending={workspaceSessions.isPending} error={workspaceSessions.error} onRetry={() => void workspaceSessions.refetch()} />
        {workspaceSessions.data && !workspaceRows.length ? <EmptyState icon={Smartphone} compact title="没有成员会话记录" description="成员登录后，设备与会话状态会显示在这里。" /> : <div className="divide-y divide-border">{workspaceRows.map((row) => <div key={textValue(row.session_id, textValue(row.id))} className="grid grid-cols-[minmax(0,1fr)_160px_100px_auto] items-center gap-4 px-4 py-3 text-[13px]"><div><p className="font-medium">{textValue(row.display_name, textValue(row.email, textValue(row.user_id)))}</p><p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{textValue(row.session_id, textValue(row.id))}</p></div><span className="text-[11px] tabular-nums text-muted-foreground">{formatDateTime((row.last_seen_at ?? row.created_at) as string | number | null | undefined)}</span><StatusBadge status={Boolean(row.active ?? true) ? "active" : "revoked"} /><Button variant="ghost" size="compact" disabled={!Boolean(row.active ?? true) || revokeWorkspace.isPending} onClick={() => revokeWorkspace.mutate(row)}>撤销</Button></div>)}</div>}
      </section> : null}
    </div>}
  </AdminPageFrame>;
}
