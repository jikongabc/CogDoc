"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { AlertTriangle, CheckCircle2, PanelRightClose } from "lucide-react";
import { useEffect, useRef, useSyncExternalStore } from "react";
import type { ChatResponse, CitationLedgerEntry, Evidence } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { SourcePreview } from "./source-preview";
import { useWorkspaceStore } from "@/stores/workspace-store";

const MOBILE_QUERY = "(max-width: 1279px)";

function subscribeToMobileChange(callback: () => void) {
  const media = window.matchMedia(MOBILE_QUERY);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}

function isMobileViewport() {
  return window.matchMedia(MOBILE_QUERY).matches;
}

function EvidenceContent({ response, ledger, evidence, onDismiss, desktop }: { response?: ChatResponse; ledger?: CitationLedgerEntry; evidence?: Evidence; onDismiss: () => void; desktop: boolean }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-[52px] shrink-0 items-center border-b border-border px-4">
        <div>
          {desktop ? <p id="evidence-panel-title" className="text-sm font-semibold">证据详情</p> : <DialogTitle className="text-sm">证据详情</DialogTitle>}
          {desktop ? <p className="text-[11px] text-muted-foreground">引用与来源的精确绑定</p> : <DialogDescription className="text-[11px]">引用与来源的精确绑定</DialogDescription>}
        </div>
        {desktop ? <Button variant="ghost" size="icon" className="ml-auto" onClick={onDismiss} aria-label="关闭证据详情"><PanelRightClose className="size-4" /></Button> : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {ledger ? <><div className={`flex items-center gap-2 border-b border-border px-4 py-2 text-xs ${response?.is_valid ? "bg-success-subtle text-success" : "bg-warning-subtle text-warning"}`}>{response?.is_valid ? <CheckCircle2 className="size-3.5" /> : <AlertTriangle className="size-3.5" />}{response?.is_valid ? "回答与引用校验通过" : "引用位置已绑定，回答整体需要审核"}</div><SourcePreview ledger={ledger} evidence={evidence} /></> : <div className="p-5 text-sm text-muted-foreground">未找到这条引用的证据详情。</div>}
      </div>
    </div>
  );
}

export function EvidencePanel({ responses }: { responses: ChatResponse[] }) {
  const open = useWorkspaceStore((state) => state.evidenceOpen);
  const selection = useWorkspaceStore((state) => state.selectedEvidence);
  const returnFocus = useWorkspaceStore((state) => state.evidenceReturnFocus);
  const close = useWorkspaceStore((state) => state.closeEvidence);
  const reduceMotion = useReducedMotion();
  const mobile = useSyncExternalStore(subscribeToMobileChange, isMobileViewport, () => false);
  const panelRef = useRef<HTMLElement>(null);
  const response = responses.find((item) => item.trace_id === selection?.traceId);
  const ledger = response?.citation_ledger.find((entry) => entry.evidence_id === selection?.evidenceId);
  const evidence = response?.evidence.find((item) => item.chunk_id === ledger?.chunk_id);

  useEffect(() => {
    if (!open || mobile) return;
    const frame = requestAnimationFrame(() => panelRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [mobile, open, selection]);

  const dismiss = () => {
    const target = returnFocus;
    close();
    requestAnimationFrame(() => {
      if (target?.isConnected) target.focus();
    });
  };

  if (mobile) {
    return (
      <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen) dismiss(); }}>
        <DialogContent className="h-[calc(100dvh-24px)] max-h-none w-[calc(100%-24px)] max-w-[520px] overflow-hidden p-0">
          <EvidenceContent response={response} ledger={ledger} evidence={evidence} onDismiss={dismiss} desktop={false} />
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <AnimatePresence initial={false}>
      {open ? (
        <motion.aside
          ref={panelRef}
          tabIndex={-1}
          initial={reduceMotion ? { width: "408px", opacity: 1 } : { width: 0, opacity: 0 }}
          animate={{ width: "408px", opacity: 1 }}
          exit={reduceMotion ? { width: 0, opacity: 1 } : { width: 0, opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.16, ease: "easeOut" }}
          className="relative z-0 shrink-0 overflow-hidden border-l border-border bg-surface"
          aria-labelledby="evidence-panel-title"
          data-motion={reduceMotion ? "reduced" : "standard"}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              dismiss();
            }
          }}
        >
          <div className="h-full w-[408px]"><EvidenceContent response={response} ledger={ledger} evidence={evidence} onDismiss={dismiss} desktop /></div>
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}
