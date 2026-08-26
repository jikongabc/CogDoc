"use client";

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { AuthSession, AuthUser, Workspace } from "@/lib/api/types";

interface SessionState {
  hydrated: boolean;
  authMode: "account" | "legacy" | null;
  accessToken: string | null;
  expiresAt: string | null;
  user: AuthUser | null;
  workspace: Workspace | null;
  selectedWorkspaceId: string | null;
  permissions: string[];
  sidebarCollapsed: boolean;
  setHydrated: (hydrated: boolean) => void;
  enterLegacy: () => void;
  setSession: (session: AuthSession) => void;
  setWorkspace: (workspace: Workspace, permissions?: string[]) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  clearSession: () => void;
}

const emptySession = {
  authMode: null,
  accessToken: null,
  expiresAt: null,
  user: null,
  workspace: null,
  selectedWorkspaceId: null,
  permissions: [],
};

export const useSessionStore = create<SessionState>()(
  persist(
    (set) => ({
      hydrated: false,
      ...emptySession,
      sidebarCollapsed: false,
      setHydrated: (hydrated) => set({ hydrated }),
      enterLegacy: () => set({ ...emptySession, authMode: "legacy" }),
      setSession: (session) =>
        set({
          authMode: "account",
          accessToken: session.access_token,
          expiresAt: session.expires_at,
          user: session.user,
          workspace: session.workspace,
          selectedWorkspaceId: session.workspace.workspace_id,
          permissions: session.permissions,
        }),
      setWorkspace: (workspace, permissions) =>
        set({
          workspace,
          selectedWorkspaceId: workspace.workspace_id,
          ...(permissions ? { permissions } : {}),
        }),
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
      clearSession: () => set(emptySession),
    }),
    {
      name: "cogdoc.session.v1",
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        accessToken: state.accessToken,
        authMode: state.authMode,
        expiresAt: state.expiresAt,
        user: state.user,
        workspace: state.workspace,
        selectedWorkspaceId: state.selectedWorkspaceId,
        permissions: state.permissions,
        sidebarCollapsed: state.sidebarCollapsed,
      }),
      onRehydrateStorage: () => (state) => state?.setHydrated(true),
    },
  ),
);
