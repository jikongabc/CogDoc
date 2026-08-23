"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { Sidebar } from "./sidebar";
import { Header } from "./header";
import { useWorkspaceStore } from "@/stores/workspace-store";

export function AppShell({ children }: { children: React.ReactNode }) {
  const mobileOpen = useWorkspaceStore((state) => state.mobileSidebarOpen);
  const setMobileOpen = useWorkspaceStore((state) => state.setMobileSidebarOpen);
  const pathname = usePathname();
  useEffect(() => setMobileOpen(false), [pathname, setMobileOpen]);
  return (
    <div className="flex h-dvh overflow-hidden bg-background">
      <a href="#main-content" className="sr-only z-[100] rounded-[3px] bg-surface px-3 py-2 focus:not-sr-only focus:fixed focus:left-3 focus:top-3">跳到主要内容</a>
      <Sidebar className="hidden md:flex" />
      <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-foreground/30 md:hidden" />
          <Dialog.Content className="fixed inset-y-0 left-0 z-50 outline-none md:hidden"><Dialog.Title className="sr-only">主导航</Dialog.Title><Sidebar forceExpanded /></Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <div className="flex min-w-0 flex-1 flex-col"><Header /><main id="main-content" className="min-h-0 flex-1 overflow-auto bg-background">{children}</main></div>
    </div>
  );
}
