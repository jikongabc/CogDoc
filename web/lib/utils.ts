import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

export function formatDateTime(value?: string | number | null) {
  if (value === undefined || value === null || value === "") return "—";
  const numeric = typeof value === "number" ? value : Number(value);
  const milliseconds = Number.isFinite(numeric)
    ? numeric < 10_000_000_000
      ? numeric * 1000
      : numeric
    : Date.parse(String(value));
  if (!Number.isFinite(milliseconds)) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(milliseconds));
}

export function formatFileType(mediaType?: string) {
  if (!mediaType) return "文件";
  const normalized = mediaType.toLowerCase();
  if (normalized.includes("pdf")) return "PDF";
  if (normalized.includes("wordprocessingml") || normalized.includes("msword")) return "DOCX";
  if (normalized.includes("spreadsheetml") || normalized.includes("excel")) return "XLSX";
  if (normalized.includes("presentationml") || normalized.includes("powerpoint")) return "PPTX";
  if (normalized.includes("markdown")) return "MD";
  if (normalized.startsWith("text/")) return normalized.split("/").at(-1)?.toUpperCase() || "文本";
  if (normalized.includes("html")) return "HTML";
  if (normalized.includes("json")) return "JSON";
  return normalized.split("/").at(-1)?.split(".").at(-1)?.toUpperCase() || "文件";
}
