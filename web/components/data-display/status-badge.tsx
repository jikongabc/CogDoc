import { Circle } from "lucide-react";
import { Badge } from "@/components/ui/badge";

const successStates = new Set(["ready", "healthy", "succeeded", "completed", "published", "approved", "active", "verified"]);
const warningStates = new Set(["pending", "running", "paused", "waiting", "degraded", "stale", "review", "needs_review"]);
const errorStates = new Set(["failed", "error", "cancelled", "blocked", "dead_letter", "revoked", "rejected"]);

const labels: Record<string, string> = {
  ready: "就绪",
  healthy: "健康",
  succeeded: "成功",
  completed: "已完成",
  published: "已发布",
  approved: "已通过",
  active: "有效",
  verified: "已核验",
  pending: "待处理",
  running: "运行中",
  paused: "已暂停",
  waiting: "等待中",
  degraded: "降级",
  stale: "需更新",
  review: "待审核",
  needs_review: "需审核",
  failed: "失败",
  error: "错误",
  cancelled: "已取消",
  blocked: "已阻止",
  dead_letter: "失败待重放",
  revoked: "已撤销",
  rejected: "已拒绝",
  draft: "草稿",
  disabled: "已停用",
  inactive: "未启用",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const normalized = status.toLowerCase();
  const variant = successStates.has(normalized)
    ? "success"
    : warningStates.has(normalized)
      ? "warning"
      : errorStates.has(normalized)
        ? "error"
        : "neutral";
  return (
    <Badge variant={variant}>
      <Circle className="size-1.5 fill-current" />
      {label || labels[normalized] || status || "未知"}
    </Badge>
  );
}
