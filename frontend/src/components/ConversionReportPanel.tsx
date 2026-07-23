import { Dispatch, SetStateAction } from "react";
import { Wrench } from "lucide-react";

import { ConversionReport } from "../api";


export type RepairChoices = Record<
  string,
  { candidatePath?: string; file?: File }
>;

type ConversionReportPanelProps = {
  report: ConversionReport;
  repairChoices: RepairChoices;
  setRepairChoices: Dispatch<SetStateAction<RepairChoices>>;
  repairing: boolean;
  onApplyRepairs: () => void;
};


export function ConversionReportPanel({
  report,
  repairChoices,
  setRepairChoices,
  repairing,
  onApplyRepairs
}: ConversionReportPanelProps) {
  return (
    <div className="conversion-report">
      <div className="report-heading">
        <strong>转换报告</strong>
        <span>
          {report.problem_count} 题 · {report.counts.warning} 警告 ·{" "}
          {report.counts.loss} 项损失
        </span>
      </div>
      {report.issues.length === 0 ? (
        <p>未发现字段损失或兼容性警告。</p>
      ) : (
        <ul>
          {report.issues.map((issue, index) => (
            <li key={`${issue.code}-${index}`} data-severity={issue.severity}>
              <strong>
                {issue.severity.toUpperCase()} ·{" "}
                {issue.problem ? `${issue.problem} · ` : ""}
                {issue.code}
              </strong>
              <span>{issue.message}</span>
            </li>
          ))}
        </ul>
      )}
      {report.repair_ready &&
        report.repair_suggestions &&
        report.repair_suggestions.length > 0 && (
          <div className="repair-assistant">
            <div className="repair-heading">
              <Wrench size={16} aria-hidden="true" />
              <strong>缺失文件修复助手</strong>
            </div>
            <p>
              系统只提供候选；选择或上传文件并点击确认后，原 ZIP
              不会被修改，将创建一个派生任务。
            </p>
            {report.repair_suggestions.map((suggestion) => {
              const choice = repairChoices[suggestion.id] || {};
              const issueContext = report.issues.find(
                (issue) =>
                  issue.code === suggestion.issue_code &&
                  issue.problem === suggestion.problem &&
                  issue.context?.expected_path === suggestion.expected_path
              )?.context;
              return (
                <div className="repair-item" key={suggestion.id}>
                  <strong>
                    {suggestion.problem ? `${suggestion.problem} · ` : ""}
                    {suggestion.expected_path}
                  </strong>
                  <small>
                    {suggestion.role} · {suggestion.issue_code}
                  </small>
                  {(issueContext?.source_location || issueContext?.source) && (
                    <small>
                      来源配置：
                      {issueContext.source_location || issueContext.source}
                    </small>
                  )}
                  {suggestion.candidates.length > 0 && (
                    <select
                      aria-label={`选择 ${suggestion.expected_path} 的包内候选`}
                      value={choice.candidatePath || ""}
                      onChange={(event) =>
                        setRepairChoices((current) => ({
                          ...current,
                          [suggestion.id]: event.target.value
                            ? { candidatePath: event.target.value }
                            : {}
                        }))
                      }
                      disabled={repairing}
                    >
                      <option value="">选择包内候选…</option>
                      {suggestion.candidates.map((candidate) => (
                        <option key={candidate.path} value={candidate.path}>
                          {candidate.path} · {Math.round(candidate.confidence * 100)}%
                        </option>
                      ))}
                    </select>
                  )}
                  <label className="repair-upload">
                    <span>或上传补充文件</span>
                    <input
                      type="file"
                      disabled={repairing}
                      onChange={(event) => {
                        const nextFile = event.target.files?.[0];
                        setRepairChoices((current) => ({
                          ...current,
                          [suggestion.id]: nextFile ? { file: nextFile } : {}
                        }));
                      }}
                    />
                  </label>
                  {choice.file && <small>已选择：{choice.file.name}</small>}
                </div>
              );
            })}
            <button
              className="primary-button"
              type="button"
              onClick={onApplyRepairs}
              disabled={repairing}
            >
              <Wrench size={17} aria-hidden="true" />
              {repairing ? "正在创建派生任务…" : "确认修复并重新转换"}
            </button>
          </div>
        )}
    </div>
  );
}
