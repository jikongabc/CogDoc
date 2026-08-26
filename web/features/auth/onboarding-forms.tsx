"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowRight } from "lucide-react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import type { AuthSession } from "@/lib/api/types";
import { api } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const registerSchema = z.object({
  displayName: z.string().trim().min(1, "请输入显示名称").max(120, "最多 120 个字符"),
  email: z.email("请输入有效邮箱"),
  workspaceName: z.string().trim().max(120, "最多 120 个字符"),
  password: z.string().min(12, "密码至少需要 12 个字符").max(256),
});

const inviteSchema = z.object({
  token: z.string().min(16, "邀请令牌格式不正确").max(512).refine((value) => value === value.trim() && !/\s/.test(value), "邀请令牌不能包含空格"),
  displayName: z.string().trim().max(120, "最多 120 个字符"),
  email: z.email("请输入受邀邮箱"),
  password: z.string().min(12, "密码至少需要 12 个字符").max(256),
});

function FieldError({ message }: { message?: string }) {
  return message ? <p className="text-xs text-error">{message}</p> : null;
}

export function RegistrationForm({ onAuthenticated }: { onAuthenticated: (session: AuthSession) => void }) {
  const form = useForm<z.infer<typeof registerSchema>>({
    resolver: zodResolver(registerSchema),
    defaultValues: { displayName: "", email: "", workspaceName: "", password: "" },
  });
  const submit = form.handleSubmit(async (values) => {
    try {
      const session = await api.register({
        display_name: values.displayName,
        email: values.email,
        password: values.password,
        ...(values.workspaceName ? { workspace_name: values.workspaceName } : {}),
      });
      onAuthenticated(session);
    } catch (error) {
      form.setError("root", { message: error instanceof Error ? error.message : "无法创建账号" });
    }
  });
  return (
    <form className="space-y-3.5" onSubmit={submit}>
      <div className="space-y-1.5"><Label htmlFor="register-name">显示名称</Label><Input id="register-name" autoComplete="name" {...form.register("displayName")} /><FieldError message={form.formState.errors.displayName?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="register-email">邮箱</Label><Input id="register-email" type="email" autoComplete="email" {...form.register("email")} /><FieldError message={form.formState.errors.email?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="register-workspace">个人工作区名称（可选）</Label><Input id="register-workspace" placeholder="例如：产品研究" {...form.register("workspaceName")} /><FieldError message={form.formState.errors.workspaceName?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="register-password">密码</Label><Input id="register-password" type="password" autoComplete="new-password" {...form.register("password")} /><FieldError message={form.formState.errors.password?.message} /></div>
      {form.formState.errors.root ? <p role="alert" className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{form.formState.errors.root.message}</p> : null}
      <Button type="submit" variant="primary" size="form" className="w-full" loading={form.formState.isSubmitting}>创建账号<ArrowRight className="size-4" /></Button>
    </form>
  );
}

export function InviteAcceptanceForm({ onAuthenticated, initialToken = "", initialEmail = "" }: { onAuthenticated: (session: AuthSession) => void; initialToken?: string; initialEmail?: string }) {
  const form = useForm<z.infer<typeof inviteSchema>>({
    resolver: zodResolver(inviteSchema),
    defaultValues: { token: initialToken, displayName: "", email: initialEmail, password: "" },
  });
  const submit = form.handleSubmit(async (values) => {
    try {
      const session = await api.acceptInvite({
        token: values.token,
        ...(values.displayName ? { display_name: values.displayName } : {}),
        email: values.email,
        password: values.password,
      });
      onAuthenticated(session);
    } catch (error) {
      form.setError("root", { message: error instanceof Error ? error.message : "邀请无效或已过期" });
    }
  });
  return (
    <form className="space-y-3.5" onSubmit={submit}>
      <div className="space-y-1.5"><Label htmlFor="invite-token">邀请令牌</Label><Input id="invite-token" type="password" autoComplete="off" {...form.register("token")} /><FieldError message={form.formState.errors.token?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="invite-name">显示名称（可选）</Label><Input id="invite-name" autoComplete="name" {...form.register("displayName")} /><FieldError message={form.formState.errors.displayName?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="invite-email">受邀邮箱</Label><Input id="invite-email" type="email" autoComplete="email" {...form.register("email")} /><FieldError message={form.formState.errors.email?.message} /></div>
      <div className="space-y-1.5"><Label htmlFor="invite-password">设置密码</Label><Input id="invite-password" type="password" autoComplete="new-password" {...form.register("password")} /><FieldError message={form.formState.errors.password?.message} /></div>
      {form.formState.errors.root ? <p role="alert" className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{form.formState.errors.root.message}</p> : null}
      <Button type="submit" variant="primary" size="form" className="w-full" loading={form.formState.isSubmitting}>接受邀请<ArrowRight className="size-4" /></Button>
    </form>
  );
}
