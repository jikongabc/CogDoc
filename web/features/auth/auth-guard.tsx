"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { BookOpenCheck, LoaderCircle } from "lucide-react";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const hydrated = useSessionStore((state) => state.hydrated);
  const authMode = useSessionStore((state) => state.authMode);
  const accessToken = useSessionStore((state) => state.accessToken);
  const setWorkspace = useSessionStore((state) => state.setWorkspace);
  const config = useQuery({ queryKey: queryKeys.authConfig, queryFn: api.authConfig, enabled: hydrated });
  const query = useQuery({ queryKey: queryKeys.me, queryFn: api.me, enabled: hydrated && authMode !== "legacy" && Boolean(accessToken) });

  useEffect(() => {
    if (!hydrated || !config.data) return;
    if (config.data.account_auth_enabled && !accessToken) router.replace("/login");
    if (!config.data.account_auth_enabled && authMode !== "legacy") router.replace("/login");
  }, [accessToken, authMode, config.data, hydrated, router]);

  useEffect(() => {
    if (query.data) setWorkspace(query.data.workspace, query.data.permissions);
  }, [query.data, setWorkspace]);

  useEffect(() => {
    if (query.error instanceof ApiError && query.error.status === 401 && authMode !== "legacy") router.replace("/login");
  }, [authMode, query.error, router]);

  const legacyReady = config.data && !config.data.account_auth_enabled && authMode === "legacy";
  const accountReady = Boolean(accessToken) && query.isSuccess;
  if (config.isError) {
    return <div className="flex h-dvh items-center justify-center bg-background px-6"><div className="max-w-sm border-l-2 border-error bg-error-subtle px-4 py-3 text-sm text-error"><p className="font-medium">无法连接 CogDoc API</p><p className="mt-1 text-xs">请确认服务正在运行，然后重新载入。</p><button className="mt-3 rounded-[5px] border border-error/30 px-2.5 py-1 text-xs font-medium hover:bg-white/40" onClick={() => void config.refetch()}>重新载入</button></div></div>;
  }
  if (query.isError && accessToken) {
    return <div className="flex h-dvh items-center justify-center bg-background px-6"><div className="max-w-sm border-l-2 border-warning bg-warning-subtle px-4 py-3 text-sm text-warning"><p className="font-medium">暂时无法验证工作区身份</p><p className="mt-1 text-xs">登录状态仍保留。请重新连接服务后重试。</p><button className="mt-3 rounded-[5px] border border-warning/30 px-2.5 py-1 text-xs font-medium hover:bg-white/40" onClick={() => void query.refetch()}>重新验证</button></div></div>;
  }
  if (!hydrated || config.isPending || (!legacyReady && !accountReady)) {
    return <div className="flex h-dvh items-center justify-center bg-background"><div className="flex items-center gap-3 text-sm text-muted-foreground"><span className="flex size-8 items-center justify-center rounded-[5px] bg-foreground text-white"><BookOpenCheck className="size-4" /></span><LoaderCircle className="size-4 animate-spin" />正在载入工作区</div></div>;
  }
  return children;
}
