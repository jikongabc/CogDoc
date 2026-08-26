"use client";

import { create } from "zustand";

interface WorkspaceUiState {
  mobileSidebarOpen: boolean;
  selectedKnowledgeBaseId: string | null;
  lastConversation: { kbId: string; sessionId: string } | null;
  localModelMode: boolean;
  evidenceOpen: boolean;
  selectedEvidence: { traceId: string; evidenceId: string } | null;
  evidenceReturnFocus: HTMLElement | null;
  setMobileSidebarOpen: (open: boolean) => void;
  setSelectedKnowledgeBaseId: (kbId: string | null) => void;
  rememberConversation: (kbId: string, sessionId: string) => void;
  clearKnowledgeContext: () => void;
  setLocalModelMode: (enabled: boolean) => void;
  openEvidence: (selection: { traceId: string; evidenceId: string }, returnFocus?: HTMLElement | null) => void;
  closeEvidence: () => void;
}

export const useWorkspaceStore = create<WorkspaceUiState>((set) => ({
  mobileSidebarOpen: false,
  selectedKnowledgeBaseId: null,
  lastConversation: null,
  localModelMode: false,
  evidenceOpen: false,
  selectedEvidence: null,
  evidenceReturnFocus: null,
  setMobileSidebarOpen: (mobileSidebarOpen) => set({ mobileSidebarOpen }),
  setSelectedKnowledgeBaseId: (selectedKnowledgeBaseId) => set({ selectedKnowledgeBaseId }),
  rememberConversation: (kbId, sessionId) => set({ lastConversation: { kbId, sessionId } }),
  clearKnowledgeContext: () => set({ selectedKnowledgeBaseId: null, lastConversation: null }),
  setLocalModelMode: (localModelMode) => set({ localModelMode }),
  openEvidence: (selectedEvidence, evidenceReturnFocus = null) => set({ evidenceOpen: true, selectedEvidence, evidenceReturnFocus }),
  closeEvidence: () => set({ evidenceOpen: false, selectedEvidence: null, evidenceReturnFocus: null }),
}));
