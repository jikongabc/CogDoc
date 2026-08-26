"use client";

import { Check } from "lucide-react";
import type { WorkspaceRole, WorkspaceRoleDefinition } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const BUILT_IN_ROLE_NAMES: WorkspaceRole[] = ["owner", "admin", "editor", "reviewer", "viewer"];

export const BUILT_IN_WORKSPACE_ROLES: WorkspaceRoleDefinition[] = BUILT_IN_ROLE_NAMES.map((roleId) => ({
  role_id: roleId,
  workspace_id: "",
  name: roleId,
  description: "",
  base_role: roleId,
  system: true,
  member_count: 0,
  revision: 0,
}));

export function roleLabel(role: Pick<WorkspaceRoleDefinition, "role_id" | "name" | "system">) {
  return role.system ? role.role_id : role.name;
}

export function RoleSelector({
  roles,
  selected,
  onChange,
  disabled = false,
  compact = false,
}: {
  roles: WorkspaceRoleDefinition[];
  selected: string[];
  onChange: (roleIds: string[]) => void;
  disabled?: boolean;
  compact?: boolean;
}) {
  const toggle = (roleId: string) => {
    onChange(
      selected.includes(roleId)
        ? selected.filter((value) => value !== roleId)
        : [...selected, roleId],
    );
  };

  if (!roles.length) {
    return <p className="text-xs text-muted-foreground">暂无可选角色</p>;
  }

  return (
    <div className={cn("overflow-hidden border border-border bg-surface", compact && "max-h-44 overflow-y-auto")}>
      {roles.map((role) => {
        const checked = selected.includes(role.role_id);
        return (
          <button
            key={role.role_id}
            type="button"
            disabled={disabled}
            onClick={() => toggle(role.role_id)}
            className="flex w-full items-center gap-3 border-b border-border px-3 py-2 text-left last:border-b-0 hover:bg-surface-subtle disabled:cursor-not-allowed disabled:opacity-60"
            aria-pressed={checked}
          >
            <span className={cn("flex size-4 shrink-0 items-center justify-center border", checked ? "border-primary bg-primary text-primary-foreground" : "border-border-strong bg-surface")}>
              {checked ? <Check className="size-3" strokeWidth={2.5} /> : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate font-mono text-[13px] font-medium">{roleLabel(role)}</span>
              {role.description ? <span className="block truncate text-[11px] text-muted-foreground">{role.description}</span> : null}
            </span>
            <span className="text-[11px] tabular-nums text-muted-foreground">{role.member_count} 人</span>
          </button>
        );
      })}
    </div>
  );
}
