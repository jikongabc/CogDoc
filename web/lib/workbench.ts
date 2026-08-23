export type WorkbenchView = "conversation" | "research" | "sources" | "knowledge" | "reviews" | "diagnostics";

export const workbenchViews: Array<{ id: WorkbenchView; label: string }> = [
  { id: "conversation", label: "对话" },
  { id: "research", label: "研究" },
  { id: "sources", label: "来源" },
  { id: "knowledge", label: "派生知识" },
  { id: "reviews", label: "证据审核" },
  { id: "diagnostics", label: "调试" },
];

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
  if (/^\/knowledge\/[^/]+\/sources/.test(pathname)) return "sources";
  if (/^\/knowledge\/[^/]+\/knowledge/.test(pathname)) return "knowledge";
  if (pathname.startsWith("/reviews")) return "reviews";
  if (/^\/knowledge\/[^/]+\/diagnostics/.test(pathname)) return "diagnostics";
  return null;
}

export function workbenchHref(view: Exclude<WorkbenchView, "conversation">, kbId: string) {
  const encoded = encodeURIComponent(kbId);
  if (view === "research") return "/research";
  if (view === "reviews") return "/reviews";
  if (view === "sources") return `/knowledge/${encoded}/sources`;
  if (view === "knowledge") return `/knowledge/${encoded}/knowledge`;
  return `/knowledge/${encoded}/diagnostics`;
}
