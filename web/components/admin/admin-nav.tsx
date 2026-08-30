"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Building2, Fingerprint, ScrollText, Shield, Users, Workflow } from "lucide-react";
import { cn } from "@/lib/utils";
import { usePermission } from "@/features/auth/permissions";
import type { Permission } from "@/lib/api/types";

const sections = [
  { href: "/admin", label: "成员与邀请", icon: Users, exact: true, permission: "manage_access" },
  { href: "/admin/identity", label: "企业身份", icon: Fingerprint, permission: "manage_access" },
  { href: "/admin/service-accounts", label: "服务账号", icon: Workflow, permission: "manage_access" },
  { href: "/admin/security", label: "会话安全", icon: Shield },
  { href: "/admin/audit", label: "审计与导出", icon: ScrollText, permission: "manage_access" },
  { href: "/admin/workspace", label: "工作区设置", icon: Building2, permission: "manage_tenant" },
] satisfies Array<{ href: string; label: string; icon: typeof Users; exact?: boolean; permission?: Permission }>;

export function AdminNav() {
  const pathname = usePathname();
  const canManageAccess = usePermission("manage_access");
  const canManageTenant = usePermission("manage_tenant");
  const canManage = canManageAccess || canManageTenant;
  const visibleSections = sections.filter(({ permission }) =>
    !permission || (permission === "manage_access" ? canManageAccess : canManageTenant));
  return (
    <nav aria-label="管理设置" className="scrollbar-none flex gap-1 overflow-x-auto border-b border-border bg-surface px-4 py-2 xl:min-h-full xl:flex-col xl:border-b-0 xl:border-r xl:px-2 xl:py-3">
      <p className="hidden px-2 pb-2 pt-1 text-[10px] font-semibold text-muted-foreground xl:block">{canManage ? "工作区设置" : "账号设置"}</p>
      {visibleSections.map(({ href, label, icon: Icon, exact }) => {
        const active = exact ? pathname === href : pathname.startsWith(href);
        return <Link key={href} href={href} className={cn("flex h-8 shrink-0 items-center gap-2 rounded-[5px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-surface-subtle hover:text-foreground", active && "bg-primary-subtle text-primary")}><Icon className="size-4" />{label}</Link>;
      })}
    </nav>
  );
}

export function AdminPageFrame({ children, allowAccountUser = false, requiredPermission = "manage_access" }: { children: React.ReactNode; allowAccountUser?: boolean; requiredPermission?: "manage_access" | "manage_tenant" }) {
  const canManageAccess = usePermission("manage_access");
  const canManageTenant = usePermission("manage_tenant");
  const authorized = requiredPermission === "manage_access" ? canManageAccess : canManageTenant;
  if (!authorized && !allowAccountUser) {
    return <div className="flex min-h-full items-center justify-center p-6"><div className="max-w-md border-l-2 border-warning bg-warning-subtle px-4 py-3 text-sm text-warning"><p className="font-medium">当前角色不能访问管理设置</p><p className="mt-1 text-xs leading-5">此页面需要 {requiredPermission === "manage_access" ? "访问管理" : "租户管理"} 权限。后端仍会独立校验每个请求。</p></div></div>;
  }
  return <div className="flex min-h-full flex-col xl:grid xl:grid-cols-[208px_minmax(0,1fr)]"><AdminNav /><div className="min-w-0 flex-1">{children}</div></div>;
}
