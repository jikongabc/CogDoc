"use client";

import type { Permission } from "@/lib/api/types";
import { useSessionStore } from "@/stores/session-store";

export function usePermission(permission: Permission) {
  return useSessionStore((state) => state.authMode === "legacy" || state.permissions.includes(permission));
}

export function useAnyPermission(permissions: Permission[]) {
  return useSessionStore((state) => state.authMode === "legacy" || permissions.some((permission) => state.permissions.includes(permission)));
}
