"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { Building2, Plus, TicketCheck, Trash2 } from "lucide-react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { AdminPageFrame } from "@/components/admin/admin-nav";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { controlApi } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";

export default function WorkspaceSettingsPage() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const workspace = useSessionStore((state) => state.workspace);
  const setWorkspace = useSessionStore((state) => state.setWorkspace);
  const setSession = useSessionStore((state) => state.setSession);
  const clearSession = useSessionStore((state) => state.clearSession);
  const workspaceId = workspace?.workspace_id || "";
  const rename = useForm({ defaultValues: { name: workspace?.name || "" } });
  const create = useForm({ defaultValues: { name: "" } });
  const invite = useForm({ defaultValues: { token: "" } });
  const [confirmName, setConfirmName] = useState("");
  const renameMutation = useMutation({ mutationFn: (name: string) => controlApi.updateWorkspace(workspaceId, name, workspace?.revision), onSuccess: (updated) => { setWorkspace(updated); toast.success("工作区名称已更新"); }, onError: (error) => rename.setError("root", { message: error.message }) });
  const createMutation = useMutation({ mutationFn: controlApi.createWorkspace, onSuccess: async (created) => { const session = await (await import("@/lib/api/client")).api.switchWorkspace(created.workspace_id); setSession(session); queryClient.clear(); router.replace("/home"); }, onError: (error) => create.setError("root", { message: error.message }) });
  const inviteMutation = useMutation({ mutationFn: controlApi.acceptAuthenticatedInvite, onSuccess: (session) => { setSession(session); queryClient.clear(); toast.success(`已加入 ${session.workspace.name}`); router.replace("/home"); }, onError: (error) => invite.setError("root", { message: error.message }) });
  const deleteMutation = useMutation({ mutationFn: () => controlApi.deleteWorkspace(workspaceId), onSuccess: () => { queryClient.clear(); clearSession(); toast.success("工作区已删除，请重新选择工作区登录"); router.replace("/login"); }, onError: (error) => toast.error(error.message) });
  return <AdminPageFrame><PageHeader eyebrow="Workspace boundary" title="工作区设置" description="管理工作区名称、创建新的数据边界，或执行受确认保护的删除。" />{!workspace ? <EmptyState icon={Building2} title="本地兼容工作区" description="本地模式使用后端默认租户，不提供账号级工作区创建和删除。" /> : <div className="mx-auto max-w-3xl space-y-6 p-4 md:p-6">
    <section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">工作区资料</h3><p className="mt-0.5 text-[11px] text-muted-foreground">ID 与数据边界保持不变。</p></div><form className="space-y-4 p-4" onSubmit={rename.handleSubmit((values) => renameMutation.mutate(values.name.trim()))}><div className="space-y-1.5"><Label htmlFor="workspace-id">工作区 ID</Label><Input id="workspace-id" value={workspaceId} disabled className="font-mono text-xs" /></div><div className="space-y-1.5"><Label htmlFor="workspace-name">名称</Label><Input id="workspace-name" {...rename.register("name", { required: true, maxLength: 120 })} /></div>{rename.formState.errors.root ? <p className="text-xs text-error">{rename.formState.errors.root.message}</p> : null}<Button type="submit" variant="primary" loading={renameMutation.isPending}>保存更改</Button></form></section>
    <section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">创建工作区</h3><p className="mt-0.5 text-[11px] text-muted-foreground">创建后会立即切换到新的独立数据边界。</p></div><form className="flex items-end gap-3 p-4" onSubmit={create.handleSubmit((values) => createMutation.mutate(values.name.trim()))}><div className="min-w-0 flex-1 space-y-1.5"><Label htmlFor="new-workspace-name">名称</Label><Input id="new-workspace-name" placeholder="例如：产品研究" {...create.register("name", { required: true, maxLength: 120 })} /></div><Button type="submit" loading={createMutation.isPending}><Plus className="size-4" />创建并切换</Button></form></section>
    <section className="border border-border bg-surface"><div className="border-b border-border px-4 py-3"><h3 className="text-[13px] font-semibold">接受工作区邀请</h3><p className="mt-0.5 text-[11px] text-muted-foreground">使用邀请令牌加入已有工作区，并立即切换数据边界。</p></div><form className="space-y-3 p-4" onSubmit={invite.handleSubmit((values) => inviteMutation.mutate(values.token.trim()))}><div className="space-y-1.5"><Label htmlFor="workspace-invite-token">邀请令牌</Label><Input id="workspace-invite-token" type="password" autoComplete="off" {...invite.register("token", { required: true, minLength: 16, maxLength: 512 })} /></div>{invite.formState.errors.root ? <p className="text-xs text-error">{invite.formState.errors.root.message}</p> : null}<Button type="submit" loading={inviteMutation.isPending}><TicketCheck className="size-4" />接受并切换</Button></form></section>
    <section className="border border-error/30 bg-surface"><div className="border-b border-error/20 px-4 py-3"><h3 className="text-[13px] font-semibold text-error">危险操作</h3><p className="mt-0.5 text-[11px] text-muted-foreground">删除工作区会遵守后端所有权、资源和审计约束。</p></div><div className="flex items-center justify-between gap-5 p-4"><div><p className="text-[13px] font-medium">删除 {workspace.name}</p><p className="mt-1 text-xs text-muted-foreground">此操作不可由前端撤销。</p></div><Dialog><DialogTrigger asChild><Button variant="destructive"><Trash2 className="size-4" />删除工作区</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>删除工作区</DialogTitle><DialogDescription>输入工作区名称 <strong>{workspace.name}</strong> 确认。后端会拒绝不满足删除条件的请求。</DialogDescription></DialogHeader><div className="space-y-1.5"><Label htmlFor="confirm-workspace">工作区名称</Label><Input id="confirm-workspace" value={confirmName} onChange={(event) => setConfirmName(event.target.value)} /></div><DialogFooter><Button variant="destructive" disabled={confirmName !== workspace.name} onClick={() => deleteMutation.mutate()} loading={deleteMutation.isPending}>确认删除</Button></DialogFooter></DialogContent></Dialog></div></section>
  </div>}</AdminPageFrame>;
}
