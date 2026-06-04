import type { JobStatus } from "../api";

const labels: Record<JobStatus, string> = {
  queued: "等待",
  running: "转换中",
  success: "成功",
  failed: "失败",
  cancelled: "已取消"
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return <span className={`status status-${status}`}>{labels[status]}</span>;
}
