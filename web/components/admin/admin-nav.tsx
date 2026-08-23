"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Building2, Fingerprint, ScrollText, Shield, Users, Workflow } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAnyPermission } from "@/features/auth/permissions";

const sections = [
  { href: "/admin", label: "成员与邀请", icon: Users, exact: true },
  { href: "/admin/identity", label: "企业身份", icon: Fingerprint },
  { href: "/admin/service-accounts", label: "服务账号", icon: Workflow },
  { href: "/admin/security", label: "会话安全", icon: Shield },
  { href: "/admin/audit", label: "审计与导出", icon: ScrollText },
  { href: "/admin/workspace", label: "工作区设置", icon: Building2 },
];

export function AdminNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="管理设置" className="flex gap-1 overflow-x-auto border-b border-border bg-surface px-4 py-2 lg:min-h-full lg:flex-col lg:border-b-0 lg:border-r lg:px-2 lg:py-3">
      <p className="hidden px-2 pb-2 pt-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground lg:block">Workspace settings</p>
      {sections.map(({ href, label, icon: Icon, exact }) => {
        const active = exact ? pathname === href : pathname.startsWith(href);
        return <Link key={href} href={href} className={cn("flex h-8 shrink-0 items-center gap-2 rounded-[5px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-surface-subtle hover:text-foreground", active && "bg-primary-subtle text-primary")}><Icon className="size-4" />{label}</Link>;
      })}
    </nav>
  );
}

export function AdminPageFrame({ children }: { children: React.ReactNode }) {
  const canManage = useAnyPermission(["manage_access", "manage_tenant"]);
  if (!canManage) {
    return <div className="flex min-h-full items-center justify-center p-6"><div className="max-w-md border-l-2 border-warning bg-warning-subtle px-4 py-3 text-sm text-warning"><p className="font-medium">当前角色不能访问管理设置</p><p className="mt-1 text-xs leading-5">需要工作区访问管理或租户管理权限。后端仍会独立校验每个请求。</p></div></div>;
  }
  return <div className="grid min-h-full lg:grid-cols-[208px_minmax(0,1fr)]"><AdminNav /><div className="min-w-0">{children}</div></div>;
}
