"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { useCreateKnowledgeBase } from "./queries";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { usePermission } from "@/features/auth/permissions";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const schema = z.object({
  kbId: z.string().trim().min(1, "请输入知识库 ID").max(56, "最多 56 个字符").regex(/^[^/\\\s]+$/, "不能包含空格或路径分隔符"),
  accessPolicy: z.enum(["workspace", "private"]),
});
type Values = z.infer<typeof schema>;

export function CreateKnowledgeBaseDialog() {
  const canWrite = usePermission("write");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const mutation = useCreateKnowledgeBase();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { kbId: "", accessPolicy: "workspace" } });
  const accessPolicy = useWatch({ control: form.control, name: "accessPolicy" });
  const submit = form.handleSubmit(async (values) => {
    try {
      const kb = await mutation.mutateAsync(values);
      toast.success(`已创建 ${kb.kb_id}`);
      setOpen(false);
      form.reset();
      router.push(`/knowledge/${encodeURIComponent(kb.kb_id)}`);
    } catch (error) { form.setError("root", { message: error instanceof Error ? error.message : "创建失败" }); }
  });
  if (!canWrite) {
    return <Tooltip><TooltipTrigger asChild><span tabIndex={0} aria-label="创建知识库需要 Write 权限" className="inline-flex"><Button variant="primary" disabled><Plus className="size-4" />创建知识库</Button></span></TooltipTrigger><TooltipContent>需要 Write 权限，请联系工作区管理员。</TooltipContent></Tooltip>;
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant="primary"><Plus className="size-4" />创建知识库</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>创建知识库</DialogTitle><DialogDescription>知识库 ID 将用于链接和 API 路径，创建后不应随意变更。</DialogDescription></DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-1.5"><Label htmlFor="kb-id">知识库 ID</Label><Input id="kb-id" placeholder="product-handbook" autoFocus {...form.register("kbId")} />{form.formState.errors.kbId ? <p className="text-xs text-error">{form.formState.errors.kbId.message}</p> : null}</div>
          <div className="space-y-1.5"><Label id="access-policy-label">初始访问范围</Label><Select value={accessPolicy} onValueChange={(value) => form.setValue("accessPolicy", value as Values["accessPolicy"], { shouldValidate: true })}><SelectTrigger className="w-full" aria-labelledby="access-policy-label"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="workspace">工作区成员</SelectItem><SelectItem value="private">仅自己和授权成员</SelectItem></SelectContent></Select><p className="text-xs text-muted-foreground">后端 ACL 仍是最终权限边界。</p></div>
          {form.formState.errors.root ? <p className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{form.formState.errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="ghost" onClick={() => setOpen(false)}>取消</Button><Button type="submit" variant="primary" loading={mutation.isPending}>创建知识库</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
