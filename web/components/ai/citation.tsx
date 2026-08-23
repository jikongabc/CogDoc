"use client";

import { FileText } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useWorkspaceStore } from "@/stores/workspace-store";

export function Citation({ traceId, evidenceId, index, source, location }: { traceId: string; evidenceId: string; index: number; source?: string; location?: string }) {
  const openEvidence = useWorkspaceStore((state) => state.openEvidence);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="mx-0.5 inline-flex h-[19px] min-w-5 translate-y-[-1px] items-center justify-center rounded-[3px] border border-primary/20 bg-primary-subtle px-1 font-mono text-[10px] font-semibold leading-none text-primary hover:border-primary/40 hover:bg-[#dfe9f6]"
          onClick={(event) => openEvidence({ traceId, evidenceId }, event.currentTarget)}
          aria-label={`打开证据 ${index}: ${source || "来源"}`}
        >
          {index}
        </button>
      </TooltipTrigger>
      <TooltipContent><span className="flex items-center gap-1.5"><FileText className="size-3" />{source || "证据"}{location ? ` · ${location}` : ""}</span></TooltipContent>
    </Tooltip>
  );
}
