"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { useCreateKnowledgeBase } from "./queries";
import { Button, type ButtonProps } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { usePermission } from "@/features/auth/permissions";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { BUILT_IN_WORKSPACE_ROLES, RoleSelector } from "@/components/access/role-selector";
import { CreateWorkspaceRoleDialog } from "@/components/access/create-role-dialog";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";

const schema = z.object({
  kbId: z.string().trim().min(1, "请输入知识库 ID").max(56, "最多 56 个字符").regex(/^[^/\\\s]+$/, "不能包含空格或路径分隔符"),
});
type Values = z.infer<typeof schema>;

export function CreateKnowledgeBaseDialog({
  triggerVariant = "primary",
  triggerSize = "default",
}: {
  triggerVariant?: ButtonProps["variant"];
  triggerSize?: ButtonProps["size"];
} = {}) {
  const canWrite = usePermission("write");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[] | null>(null);
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId) || "";
  const roles = useQuery({ queryKey: queryKeys.workspaceRoles(workspaceId), queryFn: () => api.workspaceRoles(workspaceId), enabled: open && Boolean(workspaceId) });
  const availableRoles = roles.data?.roles ?? BUILT_IN_WORKSPACE_ROLES;
  const mutation = useCreateKnowledgeBase();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { kbId: "" } });
  const submit = form.handleSubmit(async (values) => {
    const roleIds = selectedRoleIds ?? availableRoles.map((role) => role.role_id);
    if (!roleIds.length) {
      form.setError("root", { message: "请至少选择一个可访问角色" });
      return;
    }
    try {
      const kb = await mutation.mutateAsync({ kbId: values.kbId, accessPolicy: "workspace", roleIds });
      toast.success(`已创建 ${kb.kb_id}`);
      setOpen(false);
      form.reset();
      setSelectedRoleIds(null);
      router.push(`/knowledge/${encodeURIComponent(kb.kb_id)}`);
    } catch (error) { form.setError("root", { message: error instanceof Error ? error.message : "创建失败" }); }
  });
  if (!canWrite) {
    return <Tooltip><TooltipTrigger asChild><span tabIndex={0} aria-label="创建知识库需要 Write 权限" className="inline-flex"><Button variant={triggerVariant} size={triggerSize} disabled><Plus className="size-4" />创建知识库</Button></span></TooltipTrigger><TooltipContent>需要 Write 权限，请联系工作区管理员。</TooltipContent></Tooltip>;
  }
  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) setSelectedRoleIds(null); }}>
      <DialogTrigger asChild><Button variant={triggerVariant} size={triggerSize}><Plus className="size-4" />创建知识库</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>创建知识库</DialogTitle><DialogDescription>知识库 ID 将用于链接和 API 路径，创建后不应随意变更。</DialogDescription></DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5"><Label htmlFor="kb-id">知识库 ID</Label><Input id="kb-id" placeholder="product-handbook" autoFocus {...form.register("kbId")} />{form.formState.errors.kbId ? <p className="text-xs text-error">{form.formState.errors.kbId.message}</p> : null}</div>
          <div className="space-y-1.5"><div className="flex items-center justify-between gap-3"><Label>知识库可访问角色</Label><div className="flex items-center gap-2"><span className="text-[11px] text-muted-foreground">默认全部角色</span><CreateWorkspaceRoleDialog workspaceId={workspaceId} triggerVariant="ghost" /></div></div><RoleSelector roles={availableRoles} selected={selectedRoleIds ?? availableRoles.map((role) => role.role_id)} onChange={setSelectedRoleIds} compact disabled={mutation.isPending} />{roles.isError ? <p className="text-xs text-warning">暂时无法读取工作区角色；当前仅显示 owner、admin、editor、reviewer、viewer。</p> : <p className="text-xs text-muted-foreground">只有选中的角色可以发现并检索这个知识库。</p>}</div>
          {form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{form.formState.errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>创建知识库</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
