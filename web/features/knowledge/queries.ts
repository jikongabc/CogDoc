"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";
import { useSessionStore } from "@/stores/session-store";

export function useKnowledgeBases() {
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  return useQuery({ queryKey: queryKeys.knowledgeBases(workspaceId), queryFn: api.knowledgeBases });
}

export function useCreateKnowledgeBase() {
  const queryClient = useQueryClient();
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  return useMutation({
    mutationFn: ({ kbId, accessPolicy }: { kbId: string; accessPolicy: "workspace" | "private" }) => api.createKnowledgeBase(kbId, accessPolicy),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.knowledgeBases(workspaceId) });
    },
  });
}

export function useDocuments(kbId: string) {
  const workspaceId = useSessionStore((state) => state.selectedWorkspaceId);
  return useQuery({ queryKey: queryKeys.documents(workspaceId, kbId), queryFn: () => api.documents(kbId), enabled: Boolean(kbId) });
}
