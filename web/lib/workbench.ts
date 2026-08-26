export type WorkbenchView = "conversation" | "research" | "documents" | "knowledge" | "diagnostics";

export const workbenchViews: Array<{ id: WorkbenchView; label: string }> = [
  { id: "conversation", label: "对话" },
  { id: "research", label: "研究" },
  { id: "documents", label: "文档" },
  { id: "knowledge", label: "派生知识" },
  { id: "diagnostics", label: "调试" },
];

/** Compatibility route retained for bookmarks created before RAG evaluation
 * moved into the knowledge-base diagnostics workbench. */
export function isLegacyReviewsPath(pathname: string) {
  return pathname === "/reviews" || pathname.startsWith("/reviews/");
}

export function knowledgeBaseFromPath(pathname: string) {
  const match = pathname.match(/^\/knowledge\/([^/]+)/);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function sessionFromPath(pathname: string) {
  const match = pathname.match(/^\/knowledge\/[^/]+\/chat\/([^/]+)/);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function workbenchViewFromPath(pathname: string): WorkbenchView | null {
  if (/^\/knowledge\/[^/]+\/chat\//.test(pathname)) return "conversation";
  if (pathname.startsWith("/research")) return "research";
  if (/^\/knowledge\/[^/]+\/?$/.test(pathname)) return "documents";
  if (/^\/knowledge\/[^/]+\/knowledge/.test(pathname)) return "knowledge";
  if (/^\/knowledge\/[^/]+\/diagnostics/.test(pathname)) return "diagnostics";
  return null;
}

export function workbenchHref(view: Exclude<WorkbenchView, "conversation">, kbId: string) {
  const encoded = encodeURIComponent(kbId);
  if (view === "research") return "/research";
  if (view === "documents") return `/knowledge/${encoded}`;
  if (view === "knowledge") return `/knowledge/${encoded}/knowledge`;
  return `/knowledge/${encoded}/diagnostics`;
}
