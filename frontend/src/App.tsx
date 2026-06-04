import { AlertTriangle, Archive, CheckCircle2, Download, FileArchive, RotateCcw, Shield, Trash2, UploadCloud } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { deleteJob, downloadUrl, getJob, getLogs, inspectZip, JobResponse, startJob } from "./api";
import { LogViewer } from "./components/LogViewer";
import { StatusBadge } from "./components/StatusBadge";

type MissingEnv = "warn" | "error";

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [inspect, setInspect] = useState<{ job_id: string; filename: string; size: number; warnings: string[] } | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [logs, setLogs] = useState("");
  const [pidStart, setPidStart] = useState("P1000");
  const [owner, setOwner] = useState(1);
  const [tags, setTags] = useState("");
  const [only, setOnly] = useState("");
  const [runDoall, setRunDoall] = useState(false);
  const [missingEnv, setMissingEnv] = useState<MissingEnv>("warn");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRunning = job?.status === "queued" || job?.status === "running";
  const canStart = Boolean(inspect) && !isRunning && !busy;

  const validation = useMemo(() => {
    if (!/^[A-Za-z]+[0-9]+$/.test(pidStart)) return "PID 起始值应类似 P1000。";
    if (!Number.isInteger(owner) || owner < 1) return "owner 必须是正整数。";
    return null;
  }, [owner, pidStart]);

  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;

    const timer = window.setInterval(async () => {
      try {
        const [nextJob, nextLogs] = await Promise.all([getJob(job.id), getLogs(job.id)]);
        setJob(nextJob);
        setLogs(nextLogs);
      } catch (err) {
        setError(err instanceof Error ? err.message : "无法读取任务状态");
      }
    }, 1200);

    return () => window.clearInterval(timer);
  }, [job]);

  async function handleInspect() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setJob(null);
    setLogs("");
    try {
      setInspect(await inspectZip(file));
    } catch (err) {
      setInspect(null);
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleStart(event: FormEvent) {
    event.preventDefault();
    if (!inspect || validation) return;
    setBusy(true);
    setError(null);
    try {
      const nextJob = await startJob({
        job_id: inspect.job_id,
        pid_start: pidStart,
        owner,
        tags: splitList(tags),
        only: splitList(only),
        run_doall: runDoall,
        missing_env: missingEnv
      });
      setJob(nextJob);
    } catch (err) {
      setError(err instanceof Error ? err.message : "任务启动失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (inspect) {
      try {
        await deleteJob(inspect.job_id);
      } catch {
        // The job may already have been cleaned by the backend; reset the UI anyway.
      }
    }
    setFile(null);
    setInspect(null);
    setJob(null);
    setLogs("");
    setError(null);
    setRunDoall(false);
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <Archive size={24} aria-hidden="true" />
          <div>
            <h1>Polygon2Hydro Web UI</h1>
            <p>在受限 Docker runner 内转换 Polygon contest 包</p>
          </div>
        </div>
        <div className="security-chip">
          <Shield size={16} aria-hidden="true" />
          默认不执行 doall.sh
        </div>
      </header>

      <section className="workspace">
        <div className="left-column">
          <section className="panel upload-panel">
            <div className="panel-heading">
              <div>
                <h2>上传题包</h2>
                <p>仅接受 Polygon contest zip，后端只做 zip 基础校验。</p>
              </div>
              <FileArchive size={20} aria-hidden="true" />
            </div>

            <label className="drop-zone">
              <UploadCloud size={28} aria-hidden="true" />
              <span>{file ? file.name : "选择 contest.zip"}</span>
              <small>{file ? formatBytes(file.size) : "文件上传后会生成独立任务目录"}</small>
              <input
                type="file"
                accept=".zip,application/zip"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                disabled={busy || isRunning}
              />
            </label>

            <div className="button-row">
              <button className="primary-button" type="button" onClick={handleInspect} disabled={!file || busy || isRunning}>
                <UploadCloud size={17} aria-hidden="true" />
                上传并检查
              </button>
              <button className="ghost-button" type="button" onClick={handleReset} disabled={busy && !isRunning}>
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
                </div>
              </div>
            )}
          </section>

          <form className="panel config-panel" onSubmit={handleStart}>
            <div className="panel-heading">
              <div>
                <h2>转换参数</h2>
                <p>这些参数会原样传给容器内的 p2h CLI。</p>
              </div>
            </div>

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

            <label>
              <span>only slugs</span>
              <input value={only} onChange={(event) => setOnly(event.target.value)} placeholder="a, b, buy-cpu" disabled={isRunning} />
            </label>

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

            {validation && <div className="inline-error">{validation}</div>}

            <button className="primary-button wide" type="submit" disabled={!canStart || Boolean(validation)}>
              <Shield size={17} aria-hidden="true" />
              启动容器转换
            </button>
          </form>
        </div>

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
                <strong>{job?.id.slice(0, 8) || inspect?.job_id.slice(0, 8) || "-"}</strong>
              </div>
              <div>
                <span>退出码</span>
                <strong>{job?.exit_code ?? "-"}</strong>
              </div>
              <div>
                <span>开始</span>
                <strong>{job?.started_at ? new Date(job.started_at).toLocaleTimeString() : "-"}</strong>
              </div>
              <div>
                <span>结束</span>
                <strong>{job?.finished_at ? new Date(job.finished_at).toLocaleTimeString() : "-"}</strong>
              </div>
            </div>

            {job?.error && <div className="inline-error">{job.error}</div>}
            {error && <div className="inline-error">{error}</div>}

            <div className="button-row">
              <a className={`download-button ${job?.download_ready ? "" : "disabled"}`} href={job?.download_ready ? downloadUrl(job.id) : undefined}>
                <Download size={17} aria-hidden="true" />
                下载结果
              </a>
              <button className="ghost-button" type="button" onClick={handleReset} disabled={!inspect}>
                <Trash2 size={17} aria-hidden="true" />
                清理任务
              </button>
            </div>
          </section>

          <LogViewer logs={logs} />
        </div>
      </section>
    </main>
  );
}
