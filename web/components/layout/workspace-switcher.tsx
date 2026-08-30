"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronsUpDown } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/spinner";

export function WorkspaceSwitcher({ collapsed = false }: { collapsed?: boolean }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const workspace = useSessionStore((state) => state.workspace);
  const authMode = useSessionStore((state) => state.authMode);
  const setSession = useSessionStore((state) => state.setSession);
  const { data } = useQuery({ queryKey: queryKeys.me, queryFn: api.me, enabled: authMode === "account" });
  const mutation = useMutation({
    mutationFn: api.switchWorkspace,
    onSuccess: async (session) => {
      setSession(session);
      queryClient.clear();
      await queryClient.prefetchQuery({ queryKey: queryKeys.me, queryFn: api.me });
      router.push("/home");
      toast.success(`已切换到 ${session.workspace.name}`);
    },
    onError: (error) => toast.error(error.message),
  });

  if (authMode === "legacy") {
    return <div className={cn("flex h-9 items-center gap-2 rounded-[5px] px-2", collapsed && "justify-center px-0")}><span className="flex size-6 shrink-0 items-center justify-center rounded-[4px] bg-primary text-[11px] font-semibold text-white">L</span>{!collapsed ? <span><span className="block text-[13px] font-medium">本地工作区</span><span className="block text-[11px] text-muted-foreground">兼容模式</span></span> : null}</div>;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className={cn(
            "flex h-9 w-full items-center gap-2 rounded-[5px] px-2 text-left hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
            collapsed && "justify-center px-0",
          )}
          aria-label="切换工作区"
        >
          <span className="flex size-6 shrink-0 items-center justify-center rounded-[4px] bg-primary text-[11px] font-semibold text-white">
            {(workspace?.name || "C").slice(0, 1).toUpperCase()}
          </span>
          {!collapsed ? (
            <>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium">{workspace?.name || "选择工作区"}</span>
                <span className="block truncate text-[11px] text-muted-foreground">{workspace?.role || "workspace"}</span>
              </span>
              {mutation.isPending ? <Spinner size="sm" className="text-muted-foreground" /> : <ChevronsUpDown className="size-3.5 text-muted-foreground" />}
            </>
          ) : null}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="start" className="w-64">
        <DropdownMenuLabel>工作区</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {(data?.workspaces ?? (workspace ? [workspace] : [])).map((item) => (
          <DropdownMenuItem
            key={item.workspace_id}
            disabled={mutation.isPending}
            onSelect={() => item.workspace_id !== workspace?.workspace_id && mutation.mutate(item.workspace_id)}
          >
            <span className="flex size-6 items-center justify-center rounded-[4px] bg-surface-subtle text-[11px] font-semibold">{item.name.slice(0, 1).toUpperCase()}</span>
            <span className="min-w-0 flex-1"><span className="block truncate font-medium">{item.name}</span><span className="block text-[11px] text-muted-foreground">{item.role}</span></span>
            {item.workspace_id === workspace?.workspace_id ? <Check className="size-4 text-primary" /> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
