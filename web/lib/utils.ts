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

export function formatFileType(mediaType?: string) {
  if (!mediaType) return "文件";
  const subtype = mediaType.split("/").at(-1)?.toUpperCase();
  return subtype?.replace("VND.OPENXMLFORMATS-OFFICEDOCUMENT.", "") ?? "文件";
}
