"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { Button, type ButtonProps } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { usePermission } from "@/features/auth/permissions";
import { controlApi } from "@/lib/api/control-plane";
import { queryKeys } from "@/lib/query/keys";

const roleSchema = z.object({
  name: z.string().trim().min(1, "请输入角色名称").max(80, "最多 80 个字符"),
  description: z.string().trim().max(300, "最多 300 个字符"),
});

type RoleValues = z.infer<typeof roleSchema>;

export function CreateWorkspaceRoleDialog({
  workspaceId,
  triggerVariant = "secondary",
  triggerSize = "compact",
}: {
  workspaceId: string;
  triggerVariant?: ButtonProps["variant"];
  triggerSize?: ButtonProps["size"];
}) {
  const canManage = usePermission("manage_access");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const form = useForm<RoleValues>({
    resolver: zodResolver(roleSchema),
    defaultValues: { name: "", description: "" },
  });
  const mutation = useMutation({
    mutationFn: (values: RoleValues) => controlApi.createRole(workspaceId, values),
    onSuccess: async () => {
      toast.success("角色已创建");
      setOpen(false);
      form.reset();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.workspaceRoles(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: ["admin", "roles", workspaceId] }),
      ]);
    },
    onError: (error) => form.setError("root", { message: error.message }),
  });

  if (!workspaceId || !canManage) return null;

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) form.reset(); }}>
      <DialogTrigger asChild>
        <Button variant={triggerVariant} size={triggerSize}><Plus className="size-3.5" />添加角色</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加角色</DialogTitle>
          <DialogDescription>新角色默认继承 viewer 权限模板；角色名称会直接显示在成员、知识库和文档访问范围中。</DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="workspace-role-name">角色名称</Label>
            <Input id="workspace-role-name" autoFocus placeholder="例如：finance" {...form.register("name")} />
            {form.formState.errors.name ? <p className="text-xs text-error">{form.formState.errors.name.message}</p> : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="workspace-role-description">说明（可选）</Label>
            <Input id="workspace-role-description" placeholder="该角色的使用范围" {...form.register("description")} />
            {form.formState.errors.description ? <p className="text-xs text-error">{form.formState.errors.description.message}</p> : null}
          </div>
          {form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-xs text-error">{form.formState.errors.root.message}</p> : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button>
            <Button type="submit" variant="primary" loading={mutation.isPending}>添加角色</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
