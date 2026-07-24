import { Download, Trash2, XCircle } from "lucide-react";

import { ConversionReport, InspectResult, JobResponse } from "../api";
import { formatElapsed, progressLabels } from "../formatConfig";
import {
  ConversionReportPanel,
  RepairChoices
} from "./ConversionReportPanel";
import { LogViewer } from "./LogViewer";
import { StatusBadge } from "./StatusBadge";


type JobPanelProps = {
  inspect: InspectResult | null;
  job: JobResponse | null;
  logs: string;
  report: ConversionReport | null;
  repairChoices: RepairChoices;
  setRepairChoices: React.Dispatch<React.SetStateAction<RepairChoices>>;
  repairing: boolean;
  busy: boolean;
  resetting: boolean;
  error: string | null;
  isRunning: boolean;
  onCancel: () => void;
  onDownload: () => void;
  onReset: () => void;
  onApplyRepairs: () => void;
};


export function JobPanel({
  inspect,
  job,
  logs,
  report,
  repairChoices,
  setRepairChoices,
  repairing,
  busy,
  resetting,
  error,
  isRunning,
  onCancel,
  onDownload,
  onReset,
  onApplyRepairs
}: JobPanelProps) {
  const progressPercent = job?.progress?.total
    ? Math.min(
        100,
        Math.round(((job.progress.current ?? 0) / job.progress.total) * 100)
      )
    : null;

  return (
    <div className="right-column">
      <section className="panel status-panel">
        <div className="panel-heading">
          <div>
            <h2>任务状态</h2>
            <p>转换完成后会把输出目录重新打包为一个下载文件。</p>
          </div>
          {job && <StatusBadge status={job.status} />}
        </div>

        <div className="status-grid">
          <div>
            <span>Job</span>
            <strong>
              {job?.id.slice(0, 8) || inspect?.job_id.slice(0, 8) || "-"}
            </strong>
          </div>
          <div>
            <span>退出码</span>
            <strong>{job?.exit_code ?? "-"}</strong>
          </div>
          <div>
            <span>开始</span>
            <strong>
              {job?.started_at
                ? new Date(job.started_at).toLocaleTimeString()
                : "-"}
            </strong>
          </div>
          <div>
            <span>结束</span>
            <strong>
              {job?.finished_at
                ? new Date(job.finished_at).toLocaleTimeString()
                : "-"}
            </strong>
          </div>
          <div>
            <span>格式</span>
            <strong>
              {job?.source_format && job?.target_format
                ? `${job.source_format} → ${job.target_format}`
                : "-"}
            </strong>
          </div>
          <div>
            <span>字段损失</span>
            <strong>{job?.report_counts.loss ?? 0}</strong>
          </div>
        </div>

        {job?.progress && (
          <div className="job-progress" aria-live="polite">
            <div className="progress-summary">
              <strong>{progressLabels[job.progress.phase]}</strong>
              <span>
                {job.progress.problem ? `${job.progress.problem} · ` : ""}
                {progressPercent !== null ? `${progressPercent}%` : "处理中"}
              </span>
            </div>
            <div
              className={`progress-track ${progressPercent === null ? "indeterminate" : ""}`}
            >
              <span
                style={
                  progressPercent === null
                    ? undefined
                    : { width: `${progressPercent}%` }
                }
              />
            </div>
            <small>{job.progress.detail || "任务仍在运行"}</small>
            <small>
              阶段耗时{" "}
              {formatElapsed(
                job.progress.started_at,
                job.progress.last_activity_at
              )}
              {" · "}
              最后活动 {new Date(job.progress.last_activity_at).toLocaleTimeString()}
            </small>
          </div>
        )}

        <div aria-live="polite">
          {job?.error && (
            <div className="inline-error" role="alert">
              {job.error}
            </div>
          )}
          {job?.timeout && (
            <div className="inline-error" role="alert">
              超时类型：{job.timeout.kind} · 限制 {job.timeout.limit_seconds} 秒
              {job.timeout.problem ? ` · 题目 ${job.timeout.problem}` : ""}
            </div>
          )}
          {error && (
            <div className="inline-error" role="alert">
              {error}
            </div>
          )}
        </div>

        <div className="button-row">
          <button
            type="button"
            className={`download-button ${job?.download_ready ? "" : "disabled"}`}
            onClick={onDownload}
            disabled={!job?.download_ready || busy}
          >
            <Download size={17} aria-hidden="true" />
            下载结果
          </button>
          {isRunning && (
            <button
              className="danger-button"
              type="button"
              onClick={onCancel}
              disabled={busy}
            >
              <XCircle size={17} aria-hidden="true" />
              取消并保留诊断
            </button>
          )}
          <button
            className="ghost-button"
            type="button"
            onClick={onReset}
            disabled={!inspect || resetting}
          >
            <Trash2 size={17} aria-hidden="true" />
            清理任务
          </button>
        </div>

        {report && (
          <ConversionReportPanel
            report={report}
            repairChoices={repairChoices}
            setRepairChoices={setRepairChoices}
            repairing={repairing}
            onApplyRepairs={onApplyRepairs}
          />
        )}
      </section>

      <LogViewer logs={logs} />
    </div>
  );
}
