"use client";

import { useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MailPlus, MoreHorizontal, Trash2, UserRoundPlus, Users } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { AdminPageFrame } from "@/components/admin/admin-nav";
import { PageHeader } from "@/components/layout/page-header";
import { EmptyState } from "@/components/data-display/empty-state";
import { QueryState } from "@/components/data-display/query-state";
import { StatusBadge } from "@/components/data-display/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { controlApi, numberValue, records, textValue, type JsonRecord } from "@/lib/api/control-plane";
import { useSessionStore } from "@/stores/session-store";
import { roleLabel } from "@/components/access/role-selector";
import { CreateWorkspaceRoleDialog } from "@/components/access/create-role-dialog";
import { usePermission } from "@/features/auth/permissions";

const inviteSchema = z.object({ email: z.email("请输入有效邮箱"), role: z.enum(["viewer", "reviewer", "editor", "admin"]) });

function MemberRoleSelect({ roles, value, onChange, disabled = false, label = "角色" }: { roles: JsonRecord[]; value: string; onChange: (roleId: string) => void; disabled?: boolean; label?: string }) {
  const options = roles.filter((role) => textValue(role.role_id) !== "owner" || value === "owner");
  return <Select value={value} onValueChange={onChange} disabled={disabled}><SelectTrigger className="w-36" aria-label={label}><SelectValue /></SelectTrigger><SelectContent>{options.map((role) => { const id = textValue(role.role_id); return <SelectItem key={id} value={id} disabled={id === "owner"}>{roleLabel({ role_id: id, name: textValue(role.name), system: role.system === true })}</SelectItem>; })}</SelectContent></Select>;
}

function AssignRoleDialog({ workspaceId, members, roles }: { workspaceId: string; members: JsonRecord[]; roles: JsonRecord[] }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [memberId, setMemberId] = useState("");
  const [roleId, setRoleId] = useState("viewer");
  const assignableMembers = members.filter((member) => textValue(member.role) !== "owner");
  const mutation = useMutation({
    mutationFn: () => {
      const member = assignableMembers.find((item) => textValue(item.member_id, textValue(item.user_id, "")) === memberId);
      return controlApi.updateMember(workspaceId, memberId, roleId, member ? numberValue(member.revision) || undefined : undefined);
    },
    onSuccess: async () => { toast.success("成员角色已更新"); setOpen(false); setMemberId(""); await queryClient.invalidateQueries({ queryKey: ["admin", "members"] }); await queryClient.invalidateQueries({ queryKey: ["admin", "roles", workspaceId] }); },
    onError: (error) => toast.error(error.message),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Users className="size-4" />分配角色</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>为成员分配角色</DialogTitle><DialogDescription>每个成员绑定一个工作区角色。新邀请默认使用 viewer。</DialogDescription></DialogHeader><div className="space-y-4"><div className="space-y-1.5"><Label>成员</Label><Select value={memberId} onValueChange={setMemberId}><SelectTrigger><SelectValue placeholder="选择成员" /></SelectTrigger><SelectContent>{assignableMembers.map((member) => { const id = textValue(member.member_id, textValue(member.user_id, "")); return <SelectItem key={id} value={id}>{textValue(member.display_name, textValue(member.email))}</SelectItem>; })}</SelectContent></Select></div><div className="space-y-1.5"><Label>角色</Label><MemberRoleSelect roles={roles} value={roleId} onChange={setRoleId} label="分配角色" /></div></div><DialogFooter><Button variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button variant="primary" disabled={!memberId || !roleId} loading={mutation.isPending} onClick={() => mutation.mutate()}>保存角色</Button></DialogFooter></DialogContent></Dialog>;
}

function InviteDialog({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const form = useForm<z.infer<typeof inviteSchema>>({ resolver: zodResolver(inviteSchema), defaultValues: { email: "", role: "viewer" } });
  const role = useWatch({ control: form.control, name: "role" });
  const mutation = useMutation({
    mutationFn: (values: z.infer<typeof inviteSchema>) => controlApi.createInvite(workspaceId, values.email, values.role),
    onSuccess: async (value) => {
      const row = value && typeof value === "object" ? value as Record<string, unknown> : {};
      const token = typeof row.invite_token === "string" ? row.invite_token : typeof row.token === "string" ? row.token : "";
      if (token) {
        try {
          await navigator.clipboard.writeText(token);
          toast.success("邀请已创建；一次性令牌已复制到剪贴板");
        } catch {
          toast.success("邀请已创建", { description: `一次性令牌：${token}`, duration: 30_000 });
        }
      } else {
        toast.success("邀请已创建");
      }
      setOpen(false);
      form.reset();
      await queryClient.invalidateQueries({ queryKey: ["admin", "invites"] });
    },
    onError: (error) => form.setError("root", { message: error.message }),
  });
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="primary"><UserRoundPlus className="size-4" />邀请成员</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>邀请工作区成员</DialogTitle><DialogDescription>创建一个受角色约束的一次性邀请。令牌只会在创建后显示。</DialogDescription></DialogHeader><form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}><div className="space-y-4"><div className="space-y-1.5"><Label htmlFor="invite-email">邮箱</Label><Input id="invite-email" type="email" {...form.register("email")} />{form.formState.errors.email ? <p className="text-xs text-error">{form.formState.errors.email.message}</p> : null}</div><div className="space-y-1.5"><Label>角色</Label><Select value={role} onValueChange={(value) => form.setValue("role", value as z.infer<typeof inviteSchema>["role"])}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["viewer", "reviewer", "editor", "admin"].map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select></div>{form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-xs text-error">{form.formState.errors.root.message}</p> : null}</div><DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>创建邀请</Button></DialogFooter></form></DialogContent></Dialog>;
}

export default function AdminPage() {
  const queryClient = useQueryClient();
  const workspace = useSessionStore((state) => state.workspace);
  const authMode = useSessionStore((state) => state.authMode);
  const canManage = usePermission("manage_access");
  const workspaceId = workspace?.workspace_id || "";
  const members = useQuery({ queryKey: ["admin", "members", workspaceId], queryFn: () => controlApi.members(workspaceId), enabled: canManage && Boolean(workspaceId) });
  const roles = useQuery({ queryKey: ["admin", "roles", workspaceId], queryFn: () => controlApi.roles(workspaceId), enabled: canManage && Boolean(workspaceId) });
  const invites = useQuery({ queryKey: ["admin", "invites", workspaceId], queryFn: () => controlApi.invites(workspaceId), enabled: canManage && Boolean(workspaceId) });
  const memberRows = records(members.data, ["items", "members"]);
  const roleRows = records(roles.data, ["items", "roles"]);
  const inviteRows = records(invites.data, ["items", "invites"]);
  const updateRole = useMutation({
    mutationFn: ({ row, role }: { row: JsonRecord; role: string }) => controlApi.updateMember(workspaceId, textValue(row.member_id, textValue(row.user_id, "")), role, numberValue(row.revision) || undefined),
    onSuccess: async () => { toast.success("成员角色已更新"); await queryClient.invalidateQueries({ queryKey: ["admin", "members"] }); await queryClient.invalidateQueries({ queryKey: ["admin", "roles", workspaceId] }); },
    onError: (error) => toast.error(error.message),
  });
  const removeMember = useMutation({
    mutationFn: (row: JsonRecord) => controlApi.removeMember(workspaceId, textValue(row.member_id, textValue(row.user_id, ""))),
    onSuccess: async () => { toast.success("成员已移除"); await queryClient.invalidateQueries({ queryKey: ["admin", "members"] }); },
    onError: (error) => toast.error(error.message),
  });
  const revokeInvite = useMutation({
    mutationFn: (row: JsonRecord) => controlApi.revokeInvite(workspaceId, textValue(row.invite_id, textValue(row.id, ""))),
    onSuccess: async () => { toast.success("邀请已撤销"); await queryClient.invalidateQueries({ queryKey: ["admin", "invites"] }); },
    onError: (error) => toast.error(error.message),
  });
  const deleteRole = useMutation({ mutationFn: (row: JsonRecord) => controlApi.deleteRole(workspaceId, textValue(row.role_id, "")), onSuccess: async () => { toast.success("角色已删除"); await queryClient.invalidateQueries({ queryKey: ["admin", "roles", workspaceId] }); }, onError: (error) => toast.error(error.message) });
  return (
    <AdminPageFrame>
      <PageHeader eyebrow="Workspace administration" title="成员与邀请" description="管理组织成员、工作区角色和一次性邀请。" actions={workspaceId ? <><AssignRoleDialog workspaceId={workspaceId} members={memberRows} roles={roleRows} /><InviteDialog workspaceId={workspaceId} /></> : undefined} />
      {!workspaceId ? <EmptyState icon={Users} title={authMode === "legacy" ? "本地模式不提供账号目录" : "未选择工作区"} description={authMode === "legacy" ? "服务端启用账号认证后，这里会提供完整的成员、角色和邀请管理。" : "选择工作区后管理成员。"} /> : (
        <div className="space-y-6 p-4 md:p-6">
          <section className="overflow-hidden border border-border bg-surface">
            <div className="flex min-h-11 items-center justify-between gap-3 border-b border-border px-4 py-2">
              <div><h3 className="text-[13px] font-semibold">角色</h3><p className="text-[11px] text-muted-foreground">5 个内置角色不可删除；可按业务范围添加自定义角色</p></div>
              <CreateWorkspaceRoleDialog workspaceId={workspaceId} />
            </div>
            <QueryState pending={roles.isPending} error={roles.error} onRetry={() => void roles.refetch()} />
            {roleRows.length ? <div className="overflow-x-auto"><table className="w-full min-w-[520px] text-left text-[13px]"><thead className="border-b border-border bg-surface-subtle text-[11px] text-muted-foreground"><tr><th className="px-3 py-2 font-medium">角色</th><th className="px-3 py-2 text-right font-medium">用户</th><th className="w-12 px-3 py-2" /></tr></thead><tbody className="divide-y divide-border">{roleRows.map((row) => { const id = textValue(row.role_id); const system = row.system === true; const display = roleLabel({ role_id: id, name: textValue(row.name), system }); return <tr key={id} className="hover:bg-surface-subtle"><td className="px-3 py-2.5"><p className="font-mono font-medium">{display}</p>{typeof row.description === "string" && row.description ? <p className="mt-0.5 text-xs text-muted-foreground">{row.description}</p> : null}</td><td className="px-3 py-2.5 text-right tabular-nums">{numberValue(row.member_count)}</td><td className="px-3 py-2.5">{!system ? <Button variant="ghost" size="icon" className="text-error" aria-label={`删除角色 ${display}`} onClick={() => deleteRole.mutate(row)} loading={deleteRole.isPending}><Trash2 className="size-4" /></Button> : null}</td></tr>; })}</tbody></table></div> : null}
          </section>
          <section className="overflow-hidden border border-border bg-surface"><div className="flex h-11 items-center justify-between border-b border-border px-4"><div><h3 className="text-[13px] font-semibold">成员</h3><p className="text-[11px] text-muted-foreground">{memberRows.length} 个账号</p></div></div><QueryState pending={members.isPending} error={members.error} onRetry={() => void members.refetch()} />{members.data && !memberRows.length ? <EmptyState icon={Users} compact title="没有成员记录" description="创建邀请，将成员加入这个工作区。" /> : null}{memberRows.length ? <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-[13px]"><thead className="border-b border-border bg-surface-subtle text-[11px] text-muted-foreground"><tr><th className="px-3 py-2 font-medium">成员</th><th className="px-3 py-2 font-medium">角色</th><th className="px-3 py-2 font-medium">状态</th><th className="px-3 py-2 font-medium">加入时间</th><th className="w-12 px-3 py-2" /></tr></thead><tbody className="divide-y divide-border">{memberRows.map((row) => { const id = textValue(row.member_id, textValue(row.user_id)); const user = row.user && typeof row.user === "object" && !Array.isArray(row.user) ? row.user as JsonRecord : row; const roleId = textValue(row.role_id, textValue(row.role, "viewer")); const owner = textValue(row.base_role, textValue(row.role)) === "owner"; return <tr key={id} className="hover:bg-surface-subtle"><td className="px-3 py-2.5"><p className="font-medium">{textValue(user.display_name, textValue(user.name, textValue(user.email)))}</p><p className="text-xs text-muted-foreground">{textValue(user.email)}</p></td><td className="px-3 py-2.5"><MemberRoleSelect roles={roleRows} value={roleId} disabled={owner || updateRole.isPending} onChange={(role) => updateRole.mutate({ row, role })} label={`修改 ${textValue(user.display_name, textValue(user.email))} 的角色`} /></td><td className="px-3 py-2.5"><StatusBadge status={textValue(row.status, "active")} /></td><td className="px-3 py-2.5 font-mono text-[11px] text-muted-foreground">{textValue(row.joined_at, textValue(row.created_at))}</td><td className="px-3 py-2.5"><DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" aria-label="成员操作"><MoreHorizontal className="size-4" /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem className="text-error" onSelect={() => removeMember.mutate(row)}>移除成员</DropdownMenuItem></DropdownMenuContent></DropdownMenu></td></tr>; })}</tbody></table></div> : null}</section>
          <section className="overflow-hidden border border-border bg-surface"><div className="flex h-11 items-center justify-between border-b border-border px-4"><div><h3 className="text-[13px] font-semibold">待处理邀请</h3><p className="text-[11px] text-muted-foreground">邀请令牌仅在服务端允许时显示</p></div></div><QueryState pending={invites.isPending} error={invites.error} onRetry={() => void invites.refetch()} />{invites.data && !inviteRows.length ? <EmptyState icon={MailPlus} compact title="没有待处理邀请" description="邀请新成员加入工作区并分配最小权限角色。" /> : <div className="divide-y divide-border">{inviteRows.map((row) => <div key={textValue(row.invite_id, textValue(row.id))} className="grid grid-cols-[minmax(0,1fr)_100px_120px_auto] items-center gap-3 px-4 py-3 text-[13px]"><div className="min-w-0"><p className="truncate font-medium">{textValue(row.email)}</p>{typeof row.token === "string" ? <p className="mt-1 truncate font-mono text-[10px] text-warning">一次性令牌：{row.token}</p> : null}</div><span>{textValue(row.role)}</span><StatusBadge status={textValue(row.status, "pending")} /><Button variant="ghost" size="compact" onClick={() => revokeInvite.mutate(row)}>撤销</Button></div>)}</div>}</section>
        </div>
      )}
    </AdminPageFrame>
  );
}
