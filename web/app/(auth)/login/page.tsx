"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRight, BookOpenCheck, Building2, Check, Database, LoaderCircle, LockKeyhole, ShieldCheck } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { api, ApiError } from "@/lib/api/client";
import { useSessionStore } from "@/stores/session-store";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { InviteAcceptanceForm, RegistrationForm } from "@/features/auth/onboarding-forms";
import { queryKeys } from "@/lib/query/keys";

const loginSchema = z.object({
  email: z.email("请输入有效邮箱"),
  password: z.string().min(1, "请输入密码").max(256),
});
type LoginValues = z.infer<typeof loginSchema>;

function LoginContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const hydrated = useSessionStore((state) => state.hydrated);
  const authMode = useSessionStore((state) => state.authMode);
  const accessToken = useSessionStore((state) => state.accessToken);
  const setSession = useSessionStore((state) => state.setSession);
  const enterLegacy = useSessionStore((state) => state.enterLegacy);
  const [oidcStartPending, setOidcStartPending] = useState(false);
  const exchangedCodeRef = useRef<string | null>(null);
  const initialInviteToken = searchParams.get("invite") || searchParams.get("token") || "";
  const initialInviteEmail = searchParams.get("email") || "";
  const config = useQuery({ queryKey: queryKeys.authConfig, queryFn: api.authConfig });
  const form = useForm<LoginValues>({ resolver: zodResolver(loginSchema), defaultValues: { email: "", password: "" } });
  const login = useMutation({
    mutationFn: (values: LoginValues) => api.login(values.email, values.password),
    onSuccess: (session) => { setSession(session); router.replace("/home"); },
  });
  const oidcExchange = useMutation({
    mutationFn: api.exchangeOidc,
    onSuccess: (result) => {
      if (!result.session) {
        toast.error("身份服务未返回登录会话");
        return;
      }
      setSession(result.session);
      router.replace("/home");
    },
    onError: (error) => toast.error(error.message || "企业登录失败"),
  });

  useEffect(() => {
    if (!hydrated || !config.data) return;
    if (accessToken || (authMode === "legacy" && !config.data.account_auth_enabled)) {
      router.replace("/home");
    }
  }, [accessToken, authMode, config.data, hydrated, router]);

  useEffect(() => {
    const code = searchParams.get("oidc_code");
    const error = searchParams.get("oidc_error");
    if (error) {
      window.history.replaceState(window.history.state, "", "/login");
      toast.error("企业身份提供方未完成授权，请重试");
      return;
    }
    if (!code || exchangedCodeRef.current === code) return;
    exchangedCodeRef.current = code;
    window.history.replaceState(window.history.state, "", "/login");
    oidcExchange.mutate(code);
  }, [oidcExchange, router, searchParams]);

  useEffect(() => {
    if (!initialInviteToken) return;
    window.history.replaceState(window.history.state, "", "/login");
  }, [initialInviteToken]);

  const startOidc = async () => {
    try {
      setOidcStartPending(true);
      const result = await api.startOidc(`${window.location.origin}/login`);
      window.location.assign(result.authorization_url);
    } catch (error) {
      setOidcStartPending(false);
      toast.error(error instanceof Error ? error.message : "无法启动企业登录");
    }
  };

  const errorMessage = login.error instanceof ApiError && login.error.status === 401
    ? "邮箱或密码不正确"
    : login.error?.message;
  const authenticated = (session: Parameters<typeof setSession>[0]) => {
    setSession(session);
    router.replace("/home");
  };

  return (
    <main className="grid min-h-dvh bg-background lg:grid-cols-[288px_minmax(0,1fr)]">
      <section className="flex min-h-48 flex-col border-b border-border bg-surface p-6 lg:min-h-dvh lg:border-b-0 lg:border-r lg:p-8">
        <div className="flex items-center gap-2.5"><span className="flex size-8 items-center justify-center rounded-[5px] bg-foreground text-white"><BookOpenCheck className="size-[18px]" /></span><span><span className="block text-base font-semibold">CogDoc</span><span className="block text-[10px] text-muted-foreground">知识工作台</span></span></div>
        <div className="mt-10 hidden lg:block">
          <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">工作方式</p>
          <div className="mt-3 space-y-1 text-[13px]">
            {["选择工作区与知识库", "上传并管理文档", "对话、研究与证据审核"].map((item, index) => <p key={item} className="flex items-center gap-2 rounded-[4px] px-2 py-2"><span className="font-mono text-[10px] text-muted-foreground">0{index + 1}</span>{item}</p>)}
          </div>
          <div className="mt-8 border-t border-border pt-5 text-xs leading-5 text-muted-foreground"><p className="flex items-center gap-2"><ShieldCheck className="size-3.5 text-success" />账号、工作区与 ACL 由后端统一校验</p><p className="mt-2 flex items-center gap-2"><Database className="size-3.5 text-primary" />登录后恢复原有知识库工作流</p></div>
        </div>
        <p className="mt-auto hidden text-[11px] text-muted-foreground lg:block">CogDoc 2.0 · Enterprise workspace</p>
      </section>
      <section className="flex items-center justify-center p-5 sm:p-8 lg:p-12">
        <div className="w-full max-w-[420px]">
          <div className="mb-7"><div className="mb-3 flex size-9 items-center justify-center rounded-[5px] border border-border bg-surface"><LockKeyhole className="size-[18px] text-primary" /></div><h2 className="text-2xl font-semibold tracking-[-0.02em]">进入工作区</h2><p className="mt-1.5 text-sm text-muted-foreground">登录、创建账号或接受组织邀请。</p></div>
          {config.isError ? <div className="mb-4 border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error"><p>无法连接身份服务。请确认 CogDoc API 正在运行后重试。</p><Button variant="ghost" size="compact" className="mt-2 text-error" onClick={() => void config.refetch()}>重新连接</Button></div> : null}
          {oidcExchange.isPending ? <div className="flex h-36 items-center justify-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在完成企业登录</div> : config.isPending ? <div className="flex h-36 items-center justify-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取认证配置</div> : config.data && !config.data.account_auth_enabled ? (
            <div className="border border-border bg-surface">
              <div className="border-b border-border bg-surface-subtle px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-semibold"><Database className="size-4 text-primary" />本地部署模式</div>
              </div>
              <div className="p-4">
                <p className="text-[13px] leading-5 text-muted-foreground">当前服务未启用账号认证。你将以本地兼容身份进入工作台，所有数据仍由 CogDoc API 的权限策略保护。</p>
                <div className="mt-4 grid gap-2 text-xs text-muted-foreground">
                  <p className="flex items-center gap-2"><ShieldCheck className="size-3.5 text-success" />不会创建虚假的浏览器账号</p>
                  <p className="flex items-center gap-2"><Check className="size-3.5 text-success" />随时可在服务端启用账号、OIDC 与 SCIM</p>
                </div>
                <Button
                  variant="primary"
                  size="form"
                  className="mt-5 w-full"
                  onClick={() => { enterLegacy(); router.replace("/home"); }}
                >
                  进入本地工作区<ArrowRight className="size-4" />
                </Button>
              </div>
            </div>
          ) : (
            <Tabs defaultValue={initialInviteToken ? "invite" : "login"}>
              <TabsList className="mb-5 w-full gap-5">
                <TabsTrigger value="login">登录</TabsTrigger>
                <TabsTrigger value="register">注册</TabsTrigger>
                <TabsTrigger value="invite">接受邀请</TabsTrigger>
              </TabsList>
              <TabsContent value="login">
                <form className="space-y-4" onSubmit={form.handleSubmit((values) => login.mutate(values))}>
                  <div className="space-y-1.5"><Label htmlFor="email">邮箱</Label><Input id="email" type="email" autoComplete="email" placeholder="name@company.com" {...form.register("email")} />{form.formState.errors.email ? <p className="text-xs text-error">{form.formState.errors.email.message}</p> : null}</div>
                  <div className="space-y-1.5"><div className="flex items-center justify-between"><Label htmlFor="password">密码</Label></div><Input id="password" type="password" autoComplete="current-password" {...form.register("password")} />{form.formState.errors.password ? <p className="text-xs text-error">{form.formState.errors.password.message}</p> : null}</div>
                  {errorMessage ? <div role="alert" className="border-l-2 border-error bg-error-subtle px-3 py-2 text-[13px] text-error">{errorMessage}</div> : null}
                  <Button type="submit" variant="primary" size="form" className="w-full" loading={login.isPending}>登录<ArrowRight className="size-4" /></Button>
                </form>
                {config.data?.oidc_enabled ? <><div className="my-5 flex items-center gap-3 text-[11px] uppercase tracking-wide text-muted-foreground"><span className="h-px flex-1 bg-border" />或<span className="h-px flex-1 bg-border" /></div><Button variant="secondary" size="form" className="w-full" onClick={startOidc} loading={oidcStartPending}><Building2 className="size-4" />使用 {config.data.oidc_display_name || "Enterprise SSO"}</Button></> : null}
              </TabsContent>
              <TabsContent value="register">{config.data?.self_registration_enabled ? <RegistrationForm onAuthenticated={authenticated} /> : <div className="border-l-2 border-warning bg-warning-subtle px-3 py-3 text-[13px] leading-5 text-warning"><p className="font-medium">当前部署未开放自主注册</p><p className="mt-1">请使用组织提供的邀请令牌，或联系工作区管理员创建邀请。</p></div>}</TabsContent>
              <TabsContent value="invite"><InviteAcceptanceForm onAuthenticated={authenticated} initialToken={initialInviteToken} initialEmail={initialInviteEmail} /></TabsContent>
            </Tabs>
          )}
          <p className="mt-6 text-xs leading-5 text-muted-foreground">访问工作区即表示你将按照所在组织的安全、审计与会话策略使用数据。</p>
        </div>
      </section>
    </main>
  );
}

export default function LoginPage() {
  return <Suspense fallback={<main className="flex min-h-dvh items-center justify-center bg-background"><LoaderCircle className="size-5 animate-spin text-primary" /><span className="ml-2 text-sm text-muted-foreground">正在载入登录</span></main>}><LoginContent /></Suspense>;
}
