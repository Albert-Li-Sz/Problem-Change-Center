import { AlertTriangle, Archive, CheckCircle2, FileArchive, Github, RotateCcw, Shield, UploadCloud } from "lucide-react";
import { FormEvent, useMemo, useRef, useState } from "react";
import {
  ConversionReport,
  applyRepairs,
  cancelJob,
  deleteJob,
  getJob,
  getLogs,
  inspectZip,
  InspectResult,
  JobResponse,
  SourceFormat,
  startJob,
  TargetFormat
} from "./api";
import { CapabilityMatrixDialog } from "./components/CapabilityMatrixDialog";
import { RepairChoices } from "./components/ConversionReportPanel";
import { JobPanel } from "./components/JobPanel";
import {
  formatBytes,
  formatLabels,
  packageLayoutLabels,
  packageScopeLabels,
  projectUrl,
  readableFormats,
  writableFormats
} from "./formatConfig";
import { useJobLifecycle } from "./hooks/useJobLifecycle";

type MissingEnv = "warn" | "error";
type IcpcLicense = "unknown" | "public domain" | "cc0" | "cc by" | "cc by-sa" | "educational" | "permission";

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [inspectedFile, setInspectedFile] = useState<File | null>(null);
  const [inspect, setInspect] = useState<InspectResult | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [logs, setLogs] = useState("");
  const [sourceFormat, setSourceFormat] = useState<SourceFormat>("auto");
  const [targetFormat, setTargetFormat] = useState<TargetFormat>("hydro");
  const [lossPolicy, setLossPolicy] = useState<"warn" | "error">("warn");
  const [pidStart, setPidStart] = useState("P1000");
  const [owner, setOwner] = useState(1);
  const [tags, setTags] = useState("");
  const [only, setOnly] = useState("");
  const [selectionMode, setSelectionMode] = useState<"all" | "selected">("all");
  const [runDoall, setRunDoall] = useState(false);
  const [missingEnv, setMissingEnv] = useState<MissingEnv>("warn");
  const [domjudgeCodeStart, setDomjudgeCodeStart] = useState("A");
  const [domjudgeColor, setDomjudgeColor] = useState("#000000");
  const [domjudgeWithStatement, setDomjudgeWithStatement] = useState(false);
  const [domjudgeWithAttachments, setDomjudgeWithAttachments] = useState(false);
  const [domjudgeAutoValidator, setDomjudgeAutoValidator] = useState(true);
  const [domjudgeDefaultValidator, setDomjudgeDefaultValidator] = useState(false);
  const [icpcProfile, setIcpcProfile] = useState<"legacy-icpc" | "2025-09">("legacy-icpc");
  const [icpcLicense, setIcpcLicense] = useState<IcpcLicense>("unknown");
  const [icpcRightsOwner, setIcpcRightsOwner] = useState("");
  const [fpsProfile, setFpsProfile] = useState<"hustoj-1.6" | "qduoj-1.2">("hustoj-1.6");
  const [report, setReport] = useState<ConversionReport | null>(null);
  const [repairChoices, setRepairChoices] = useState<RepairChoices>({});
  const [repairing, setRepairing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const capabilityDialogRef = useRef<HTMLDialogElement>(null);
  const requestEpoch = useRef(0);

  const isRunning = job?.status === "queued" || job?.status === "running";
  const canStart = Boolean(inspect && file && inspectedFile === file) && !isRunning && !busy && !resetting;
  const effectiveSource = sourceFormat === "auto" ? inspect?.detected_format ?? null : sourceFormat;
  const targetFormats =
    inspect && effectiveSource === inspect.detected_format
      ? inspect.supported_targets
      : writableFormats.filter((format) => format !== effectiveSource);
  const usesHydroOutputOptions = targetFormat === "hydro";
  const usesDomjudgeOutputOptions = targetFormat === "icpc";
  const usesPolygonSource = effectiveSource === "polygon";
  const domjudgeColorPickerValue = /^#[0-9A-Fa-f]{6}$/.test(domjudgeColor) ? domjudgeColor : "#000000";
  const openCapabilityMatrix = () => {
    const dialog = capabilityDialogRef.current;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  };

  const closeCapabilityMatrix = () => {
    const dialog = capabilityDialogRef.current;
    if (!dialog) return;
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  };

  const validation = useMemo(() => {
    if (inspect && sourceFormat === "auto" && !inspect.detected_format) {
      return "自动识别未达到置信阈值，请手动选择输入格式。";
    }
    if (selectionMode === "selected" && splitList(only).length === 0) {
      return "指定题目模式下至少选择一道题目。";
    }
    if (effectiveSource === targetFormat) return "输入和输出格式不能相同。";
    if (targetFormat === "hydro") {
      if (!/^[A-Za-z]+[0-9]+$/.test(pidStart)) return "PID 起始值应类似 P1000。";
      if (!Number.isInteger(owner) || owner < 1) return "owner 必须是正整数。";
    }
    if (targetFormat === "icpc") {
      if (!/^[A-Za-z]+$/.test(domjudgeCodeStart)) return "DOMjudge 短名起始值应类似 A。";
      if (!/^#[0-9A-Fa-f]{6}$/.test(domjudgeColor)) return "DOMjudge 颜色必须是 #RRGGBB。";
      if (!["unknown", "public domain"].includes(icpcLicense) && !icpcRightsOwner.trim()) {
        return "所选 ICPC 许可证需要填写 rights owner。";
      }
      if (icpcLicense === "public domain" && icpcRightsOwner.trim()) {
        return "public domain 题包不能设置 rights owner。";
      }
    }
    if (usesPolygonSource && targetFormat === "icpc") {
      if (domjudgeAutoValidator && domjudgeDefaultValidator) return "自动识别 checker 和强制默认 validator 不能同时启用。";
    }
    return null;
  }, [domjudgeAutoValidator, domjudgeCodeStart, domjudgeColor, domjudgeDefaultValidator, effectiveSource, icpcLicense, icpcRightsOwner, inspect, only, owner, pidStart, selectionMode, sourceFormat, targetFormat, usesPolygonSource]);

  useJobLifecycle({
    job,
    requestEpoch,
    setJob,
    setLogs,
    setReport,
    setError
  });
  async function handleInspect() {
    if (!file) return;
    const selectedFile = file;
    const epoch = ++requestEpoch.current;
    setBusy(true);
    setError(null);
    setJob(null);
    setLogs("");
    setReport(null);
    try {
      const nextInspect = await inspectZip(selectedFile);
      if (requestEpoch.current !== epoch) return;
      setInspect(nextInspect);
      setInspectedFile(selectedFile);
      setOnly("");
      setSelectionMode("all");
      if (
        nextInspect.detected_format === targetFormat &&
        nextInspect.supported_targets.length > 0
      ) {
        setTargetFormat(nextInspect.supported_targets[0]);
      }
    } catch (err) {
      if (requestEpoch.current !== epoch) return;
      setInspect(null);
      setInspectedFile(null);
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      if (requestEpoch.current === epoch) setBusy(false);
    }
  }

  async function handleStart(event: FormEvent) {
    event.preventDefault();
    if (!inspect || !file || inspectedFile !== file || validation) return;
    const epoch = requestEpoch.current;
    setBusy(true);
    setError(null);
    try {
      const nextJob = await startJob({
        job_id: inspect.job_id,
        source_format: sourceFormat,
        target_format: targetFormat,
        loss_policy: lossPolicy,
        only: splitList(only),
        options: {
          polygon: {
            run_doall: usesPolygonSource && runDoall,
            missing_env: missingEnv,
            with_statement: domjudgeWithStatement,
            with_attachments: domjudgeWithAttachments,
            validator_mode: domjudgeDefaultValidator ? "default" : domjudgeAutoValidator ? "auto" : "custom"
          },
          hydro: { pid_start: pidStart, owner, tags: splitList(tags) },
          icpc: {
            code_start: domjudgeCodeStart,
            color: domjudgeColor,
            profile: icpcProfile,
            license: icpcLicense,
            rights_owner: icpcRightsOwner.trim()
          },
          fps: { profile: fpsProfile }
        }
      });
      if (requestEpoch.current !== epoch) return;
      setJob(nextJob);
    } catch (err) {
      if (requestEpoch.current !== epoch) return;
      setError(err instanceof Error ? err.message : "任务启动失败");
    } finally {
      if (requestEpoch.current === epoch) setBusy(false);
    }
  }

  async function handleReset() {
    const jobId = job?.id ?? inspect?.job_id;
    const epoch = ++requestEpoch.current;
    setResetting(true);
    setBusy(false);
    setFile(null);
    setInspectedFile(null);
    setInspect(null);
    setJob(null);
    setLogs("");
    setReport(null);
    setRepairChoices({});
    setError(null);
    setPidStart("P1000");
    setOwner(1);
    setTags("");
    setOnly("");
    setSelectionMode("all");
    setRunDoall(false);
    setMissingEnv("warn");
    setSourceFormat("auto");
    setTargetFormat("hydro");
    setLossPolicy("warn");
    setDomjudgeCodeStart("A");
    setDomjudgeColor("#000000");
    setDomjudgeWithStatement(false);
    setDomjudgeWithAttachments(false);
    setDomjudgeAutoValidator(true);
    setDomjudgeDefaultValidator(false);
    setIcpcProfile("legacy-icpc");
    setIcpcLicense("unknown");
    setIcpcRightsOwner("");
    setFpsProfile("hustoj-1.6");
    setRepairing(false);
    if (jobId) {
      try {
        await deleteJob(jobId);
      } catch {
        // The job may already have been cleaned by the backend; reset the UI anyway.
      }
    }
    if (requestEpoch.current === epoch) setResetting(false);
  }

  function handleFileChange(nextFile: File | null) {
    requestEpoch.current += 1;
    const previousJobId = job?.id ?? inspect?.job_id;
    setFile(nextFile);
    setInspectedFile(null);
    setInspect(null);
    setJob(null);
    setLogs("");
    setReport(null);
    setRepairChoices({});
    setError(null);
    setOnly("");
    setSelectionMode("all");
    if (previousJobId && !isRunning) {
      void deleteJob(previousJobId).catch(() => {
        // Invalidating the old inspection is sufficient even if backend cleanup races with TTL cleanup.
      });
    }
  }

  async function handleCancel() {
    if (!job || !isRunning) return;
    const epoch = requestEpoch.current;
    setBusy(true);
    setError(null);
    try {
      await cancelJob(job.id);
      const [nextJob, nextLogs] = await Promise.all([getJob(job.id), getLogs(job.id)]);
      if (requestEpoch.current !== epoch) return;
      setJob(nextJob);
      setLogs(nextLogs);
    } catch (err) {
      if (requestEpoch.current !== epoch) return;
      setError(err instanceof Error ? err.message : "取消任务失败");
    } finally {
      if (requestEpoch.current === epoch) setBusy(false);
    }
  }

  async function handleApplyRepairs() {
    if (!job || !report?.repair_suggestions?.length) return;
    const selections: Array<{ suggestion_id: string; candidate_path?: string; upload_name?: string }> = [];
    const files: File[] = [];
    for (const suggestion of report.repair_suggestions) {
      const choice = repairChoices[suggestion.id];
      if (choice?.candidatePath) {
        selections.push({ suggestion_id: suggestion.id, candidate_path: choice.candidatePath });
      } else if (choice?.file) {
        selections.push({ suggestion_id: suggestion.id, upload_name: choice.file.name });
        files.push(choice.file);
      }
    }
    if (!selections.length) {
      setError("请至少确认一个缺失文件修复。");
      return;
    }
    const epoch = requestEpoch.current;
    setRepairing(true);
    setError(null);
    try {
      const nextJob = await applyRepairs(job.id, selections, files);
      if (requestEpoch.current !== epoch) return;
      setJob(nextJob);
      setLogs("");
      setReport(null);
      setRepairChoices({});
    } catch (err) {
      if (requestEpoch.current !== epoch) return;
      setError(err instanceof Error ? err.message : "应用修复失败");
    } finally {
      if (requestEpoch.current === epoch) setRepairing(false);
    }
  }

  return (
    <div className="site-shell">
      <nav className="app-navigation" aria-label="主导航">
        <div className="navigation-inner">
          <div className="navigation-brand">
            <span className="navigation-brand-mark">
              <Archive size={18} aria-hidden="true" />
            </span>
            <span>Problem Change Center</span>
          </div>
          <div className="navigation-tabs">
            <span className="navigation-tab active" aria-current="page">题包转换</span>
            <button
              className="navigation-tab"
              type="button"
              aria-label="查看格式能力矩阵"
              onClick={openCapabilityMatrix}
            >
              格式能力
            </button>
            <a
              className="navigation-tab"
              href={projectUrl}
              target="_blank"
              rel="noreferrer"
            >
              <Github size={15} aria-hidden="true" />
              项目
            </a>
          </div>
          <div className="navigation-status" title="默认不执行 doall.sh">
            <Shield size={15} aria-hidden="true" />
            安全模式
          </div>
        </div>
      </nav>

      <main className="app-shell">
        <header className="topbar">
          <div className="brand">
            <h1>OJ 题包转换器</h1>
            <p>在隔离环境中转换主流 OJ 与 ICPC 标准题包</p>
          </div>
          <div className="security-chip">
            <span className="security-indicator" aria-hidden="true" />
            <div>
              <strong>隔离运行</strong>
              <span>默认不执行 doall.sh</span>
            </div>
          </div>
        </header>

        <section className="workspace">
          <div className="left-column">
            <section className="panel upload-panel">
            <div className="panel-heading">
              <div>
                <h2>上传题包</h2>
                <p>接受 Polygon、ProbHub、HydroOJ、ICPC、HOJ、FPS、QDUOJ、UOJ、DMOJ 或通用 zip。</p>
              </div>
              <FileArchive size={20} aria-hidden="true" />
            </div>

            <label className="drop-zone">
              <UploadCloud size={28} aria-hidden="true" />
              <span>{file ? file.name : "选择题包 zip"}</span>
              <small>{file ? formatBytes(file.size) : "文件上传后会生成独立任务目录"}</small>
              <input
                type="file"
                accept=".zip,application/zip"
                onChange={(event) => handleFileChange(event.target.files?.[0] ?? null)}
                disabled={busy || isRunning || resetting}
              />
            </label>

            <div className="button-row">
              <button className="primary-button" type="button" onClick={handleInspect} disabled={!file || busy || isRunning || resetting}>
                <UploadCloud size={17} aria-hidden="true" />
                上传并检查
              </button>
              <button className="ghost-button" type="button" onClick={handleReset} disabled={resetting || (busy && !isRunning)}>
                <RotateCcw size={17} aria-hidden="true" />
                重新开始
              </button>
            </div>

            {inspect && (
              <div className="upload-result">
                <CheckCircle2 size={18} aria-hidden="true" />
                <div>
                  <strong>{inspect.filename}</strong>
                  <span>{formatBytes(inspect.size)} · Job {inspect.job_id.slice(0, 8)}</span>
                  <span>
                    {inspect.detected_format
                      ? `识别为 ${formatLabels[inspect.detected_format]}`
                      : "未唯一识别，请手动选择输入格式"}
                  </span>
                  <span>
                    {packageScopeLabels[inspect.package_scope]}
                    {inspect.problem_count !== null ? ` · ${inspect.problem_count} 题` : ""}
                    {` · ${packageLayoutLabels[inspect.package_layout]}`}
                  </span>
                </div>
              </div>
            )}
            {inspect && (inspect.warnings.length > 0 || (!inspect.detected_format && inspect.format_candidates.length > 0)) && (
              <div className="inspection-details" aria-live="polite">
                {inspect.warnings.length > 0 && (
                  <ul>
                    {inspect.warnings.map((warning) => <li key={warning}>{warning}</li>)}
                  </ul>
                )}
                {!inspect.detected_format && inspect.format_candidates.length > 0 && (
                  <div>
                    <strong>识别候选</strong>
                    <ul>
                      {inspect.format_candidates.map((candidate) => (
                        <li key={candidate.format}>
                          {formatLabels[candidate.format]} · {Math.round(candidate.confidence * 100)}%
                          {candidate.evidence.length > 0 ? ` · ${candidate.evidence.join("；")}` : ""}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
            <CapabilityMatrixDialog
              ref={capabilityDialogRef}
              onClose={closeCapabilityMatrix}
            />
            </section>

            <form className="panel config-panel" onSubmit={handleStart}>
            <div className="panel-heading">
              <div>
                <h2>转换参数</h2>
                <p>选择输入与输出格式后，只会显示该方向会使用的参数。</p>
              </div>
            </div>

            <div className="format-selectors" aria-label="转换方向">
              <label>
                <span>输入格式</span>
                <select
                  value={sourceFormat}
                  onChange={(event) => {
                    const nextSource = event.target.value as SourceFormat;
                    const nextEffective =
                      nextSource === "auto" ? inspect?.detected_format ?? null : nextSource;
                    setSourceFormat(nextSource);
                    if (nextEffective === targetFormat) {
                      const nextTarget = writableFormats.find(
                        (format) => format !== nextEffective
                      );
                      if (nextTarget) setTargetFormat(nextTarget);
                    }
                  }}
                  disabled={isRunning}
                >
                  <option value="auto">自动识别{inspect?.detected_format ? ` · ${formatLabels[inspect.detected_format]}` : ""}</option>
                  {readableFormats.map((format) => (
                    <option key={format} value={format}>{formatLabels[format]}</option>
                  ))}
                </select>
              </label>
              <span className="format-arrow" aria-hidden="true">→</span>
              <label>
                <span>输出格式</span>
                <select
                  value={targetFormat}
                  onChange={(event) => setTargetFormat(event.target.value as TargetFormat)}
                  disabled={isRunning}
                >
                  {targetFormats.map((format) => (
                    <option key={format} value={format}>
                      {formatLabels[format]}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <label>
              <span>有损转换策略</span>
              <select value={lossPolicy} onChange={(event) => setLossPolicy(event.target.value as "warn" | "error")} disabled={isRunning}>
                <option value="warn">warn · 输出结果并列出损失</option>
                <option value="error">error · 发现字段损失立即失败</option>
              </select>
            </label>

            {usesHydroOutputOptions && (
              <>
                <div className="form-grid">
                  <label>
                    <span>PID 起始值</span>
                    <input value={pidStart} onChange={(event) => setPidStart(event.target.value)} disabled={isRunning} />
                  </label>
                  <label>
                    <span>owner</span>
                    <input
                      type="number"
                      min={1}
                      value={owner}
                      onChange={(event) => setOwner(Number(event.target.value))}
                      disabled={isRunning}
                    />
                  </label>
                </div>

                <label>
                  <span>tags</span>
                  <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="校赛, 2026" disabled={isRunning} />
                </label>

                {effectiveSource === "icpc" && (
                  <div className="format-note">
                    <strong>PDF 题面</strong>
                    <span>DOMjudge 的 problem_statement/*.pdf 会写入 additional_file，并通过 @[pdf](file://文件名) 作为 Hydro 题面。</span>
                  </div>
                )}

                {effectiveSource === "hoj" && (
                  <div className="format-note">
                    <strong>HOJ 原生题包</strong>
                    <span>读取成对的 problem_x.json 与 problem_x/ 测试数据目录，并保留 OI 分组、文件 IO、SPJ 和交互题配置。</span>
                  </div>
                )}
              </>
            )}

            {usesDomjudgeOutputOptions && (
              <div className="domjudge-options">
                <div className="form-grid">
                  <label>
                    <span>短名起始值</span>
                    <input value={domjudgeCodeStart} onChange={(event) => setDomjudgeCodeStart(event.target.value)} disabled={isRunning} />
                  </label>
                  <label>
                    <span>题目颜色</span>
                    <div className="color-control">
                      <input
                        aria-label="选择 DOMjudge 题目颜色"
                        type="color"
                        value={domjudgeColorPickerValue}
                        onChange={(event) => setDomjudgeColor(event.target.value)}
                        disabled={isRunning}
                      />
                      <input value={domjudgeColor} onChange={(event) => setDomjudgeColor(event.target.value)} disabled={isRunning} />
                    </div>
                  </label>
                </div>

                {usesPolygonSource && (
                  <>
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={domjudgeAutoValidator}
                        onChange={(event) => setDomjudgeAutoValidator(event.target.checked)}
                        disabled={isRunning || domjudgeDefaultValidator}
                      />
                      <span>自动识别 Polygon 标准 checker，并替换为 DOMjudge 默认 validator</span>
                    </label>

                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={domjudgeDefaultValidator}
                        onChange={(event) => setDomjudgeDefaultValidator(event.target.checked)}
                        disabled={isRunning || domjudgeAutoValidator}
                      />
                      <span>强制使用 DOMjudge 默认 validator</span>
                    </label>

                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={domjudgeWithStatement}
                        onChange={(event) => setDomjudgeWithStatement(event.target.checked)}
                        disabled={isRunning}
                      />
                      <span>包含 Polygon 包内 PDF statement</span>
                    </label>

                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={domjudgeWithAttachments}
                        onChange={(event) => setDomjudgeWithAttachments(event.target.checked)}
                        disabled={isRunning}
                      />
                      <span>包含 Polygon attachments</span>
                    </label>
                  </>
                )}

                <label>
                  <span>ICPC 包规范</span>
                  <select value={icpcProfile} onChange={(event) => setIcpcProfile(event.target.value as "legacy-icpc" | "2025-09")} disabled={isRunning}>
                    <option value="legacy-icpc">legacy-icpc · DOMjudge/Kattis 兼容</option>
                    <option value="2025-09">2025-09 · 新标准（需要 accepted solution）</option>
                  </select>
                </label>

                <div className="form-grid">
                  <label>
                    <span>题包许可证</span>
                    <select
                      value={icpcLicense}
                      onChange={(event) => {
                        const value = event.target.value as IcpcLicense;
                        setIcpcLicense(value);
                        if (value === "public domain") setIcpcRightsOwner("");
                      }}
                      disabled={isRunning}
                    >
                      <option value="unknown">unknown · 未声明</option>
                      <option value="public domain">public domain</option>
                      <option value="cc0">CC0</option>
                      <option value="cc by">CC BY 4.0+</option>
                      <option value="cc by-sa">CC BY-SA 4.0+</option>
                      <option value="educational">educational</option>
                      <option value="permission">permission</option>
                    </select>
                  </label>
                  <label>
                    <span>rights owner</span>
                    <input
                      value={icpcRightsOwner}
                      onChange={(event) => setIcpcRightsOwner(event.target.value)}
                      placeholder="版权方名称"
                      disabled={isRunning || icpcLicense === "public domain"}
                    />
                  </label>
                </div>
              </div>
            )}

            {targetFormat === "hoj" && (
              <div className="format-note">
                <strong>HOJ 导入结构</strong>
                <span>输出 zip 顶层为成对的 problem_x.json 与 problem_x/ 目录，可直接用于 HOJ 后台题目导入。</span>
              </div>
            )}

            {targetFormat === "fps" && (
              <label>
                <span>FPS 兼容 profile</span>
                <select value={fpsProfile} onChange={(event) => setFpsProfile(event.target.value as "hustoj-1.6" | "qduoj-1.2")} disabled={isRunning}>
                  <option value="hustoj-1.6">HUSTOJ / OpenJudger · FPS 1.6</option>
                  <option value="qduoj-1.2">QDUOJ FPS 兼容 · FPS 1.2</option>
                </select>
              </label>
            )}

            {(targetFormat === "uoj" || targetFormat === "dmoj") && (
              <div className="format-note">
                <strong>评测数据包</strong>
                <span>输出包含可上传的评测数据 zip，以及独立题面和 metadata sidecar；站点题库记录仍需在目标 OJ 中创建。</span>
              </div>
            )}

            {inspect?.package_scope === "multi" && (
              <div className="problem-selection">
                <label>
                  <span>转换范围</span>
                  <select
                    value={selectionMode}
                    onChange={(event) => {
                      const mode = event.target.value as "all" | "selected";
                      setSelectionMode(mode);
                      if (mode === "all") setOnly("");
                    }}
                    disabled={isRunning}
                  >
                    <option value="all">全部 {inspect.problem_count ?? ""} 题</option>
                    <option value="selected">指定题目</option>
                  </select>
                </label>

                {selectionMode === "selected" && inspect.problems.length > 0 && !inspect.problems_truncated && (
                  <fieldset className="problem-choice-list">
                    <legend>题目</legend>
                    {inspect.problems.map((problem) => {
                      const selected = splitList(only).includes(problem.id);
                      return (
                        <label className="checkbox-row" key={`${problem.id}:${problem.path}`}>
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={(event) => {
                              const values = splitList(only);
                              const next = event.target.checked
                                ? [...values, problem.id]
                                : values.filter((value) => value !== problem.id);
                              setOnly(Array.from(new Set(next)).join(", "));
                            }}
                            disabled={isRunning}
                          />
                          <span>{problem.id}</span>
                          <small>{problem.path}</small>
                        </label>
                      );
                    })}
                  </fieldset>
                )}

                {selectionMode === "selected" && (inspect.problems.length === 0 || inspect.problems_truncated) && (
                  <label>
                    <span>题目标识</span>
                    <input
                      value={only}
                      onChange={(event) => setOnly(event.target.value)}
                      placeholder="a, b, buy-cpu"
                      disabled={isRunning}
                      list="detected-problem-options"
                    />
                    <datalist id="detected-problem-options">
                      {inspect.problems.map((problem) => (
                        <option value={problem.id} key={`${problem.id}:${problem.path}`} />
                      ))}
                    </datalist>
                  </label>
                )}
              </div>
            )}

            {inspect?.package_scope === "unknown" && (
              <label>
                <span>题目标识（可选）</span>
                <input value={only} onChange={(event) => setOnly(event.target.value)} placeholder="a, b, buy-cpu" disabled={isRunning} />
              </label>
            )}

            {usesPolygonSource && (
              <>
                <div className="safety-box">
                  <div className="safety-title">
                    <AlertTriangle size={18} aria-hidden="true" />
                    doall.sh 执行策略
                  </div>
                  <p>安全模式会强制使用 --no-run-doall。启用脚本执行后，脚本仍会被限制在无网络、非 root、限资源的 Docker 容器中。</p>
                  <p>如果题包运行 Windows .exe，请先构建 p2h-runner-wine，并用 P2H_RUNNER_IMAGE=p2h-runner-wine 启动后端。</p>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={runDoall} onChange={(event) => setRunDoall(event.target.checked)} disabled={isRunning} />
                    <span>我信任该 Polygon 包，并允许在隔离容器内执行 doall.sh</span>
                  </label>
                </div>

                <label>
                  <span>缺失环境策略</span>
                  <select value={missingEnv} onChange={(event) => setMissingEnv(event.target.value as MissingEnv)} disabled={isRunning}>
                    <option value="warn">warn · 记录警告后继续</option>
                    <option value="error">error · 缺依赖时直接失败</option>
                  </select>
                </label>
              </>
            )}

            {validation && <div className="inline-error" role="alert">{validation}</div>}

            <button className="primary-button wide" type="submit" disabled={!canStart || Boolean(validation)}>
              <Shield size={17} aria-hidden="true" />
              启动容器转换
            </button>
            </form>
          </div>

          <JobPanel
            inspect={inspect}
            job={job}
            logs={logs}
            report={report}
            repairChoices={repairChoices}
            setRepairChoices={setRepairChoices}
            repairing={repairing}
            busy={busy}
            resetting={resetting}
            error={error}
            isRunning={isRunning}
            onCancel={handleCancel}
            onReset={handleReset}
            onApplyRepairs={handleApplyRepairs}
          />
        </section>
      </main>
      <footer className="site-footer">
        <div className="site-footer-inner">
          <span>Copyright © 2026 Albert_Li · MIT License</span>
          <a href={projectUrl} target="_blank" rel="noreferrer">
            <Github size={15} aria-hidden="true" />
            Problem Change Center
          </a>
        </div>
      </footer>
    </div>
  );
}
