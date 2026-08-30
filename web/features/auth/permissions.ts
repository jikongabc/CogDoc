"use client";

import type { Permission } from "@/lib/api/types";
import { useSessionStore } from "@/stores/session-store";

export function usePermission(permission: Permission) {
  return useSessionStore((state) => state.authMode === "legacy" || state.permissions.includes(permission));
}
