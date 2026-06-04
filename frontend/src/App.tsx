import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Download,
  FileArchive,
  KeyRound,
  LogIn,
  LogOut,
  RefreshCw,
  RotateCcw,
  Save,
  Shield,
  Trash2,
  UploadCloud,
  Users
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  deleteJob,
  downloadJob,
  getJob,
  getLogs,
  getMe,
  inspectZip,
  JobResponse,
  listUsers,
  login,
  register,
  resetUserPassword,
  startJob,
  TargetFormat,
  updateUser,
  UserResponse,
  changePassword
} from "./api";
import { LogViewer } from "./components/LogViewer";
import { StatusBadge } from "./components/StatusBadge";

type MissingEnv = "warn" | "error";
type AuthMode = "login" | "register";

const tokenStorageKey = "p2h_auth_token";

const targetOptions: Array<{ value: TargetFormat; label: string; hint: string }> = [
  { value: "hydro", label: "Polygon -> HydroOJ", hint: "Polygon contest zip" },
  { value: "domjudge", label: "Polygon -> DOMjudge", hint: "Polygon contest zip" },
  { value: "hydro_to_domjudge", label: "HydroOJ -> DOMjudge", hint: "HydroOJ package zip" }
];

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

function quotaText(user: UserResponse): string {
  if (user.is_admin) return "管理员 · 无限转换";
  const pending = user.daily_pending > 0 ? ` · 排队 ${user.daily_pending}` : "";
  return `今日剩余 ${user.remaining_today ?? 0} / ${user.daily_quota}${pending}`;
}

function approvalStatusText(status: UserResponse["approval_status"]): string {
  if (status === "approved") return "已通过";
  if (status === "rejected") return "已拒绝";
  return "待审核";
}

export default function App() {
  const [token, setToken] = useState(() => window.localStorage.getItem(tokenStorageKey) || "");
  const [user, setUser] = useState<UserResponse | null>(null);
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authNotice, setAuthNotice] = useState<string | null>(null);

  const [adminUsers, setAdminUsers] = useState<UserResponse[]>([]);
  const [adminBusy, setAdminBusy] = useState(false);
  const [adminError, setAdminError] = useState<string | null>(null);
  const [adminNotice, setAdminNotice] = useState<string | null>(null);
  const [adminPasswordDrafts, setAdminPasswordDrafts] = useState<Record<string, string>>({});

  const [passwordPanelOpen, setPasswordPanelOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordNotice, setPasswordNotice] = useState<string | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [inspect, setInspect] = useState<{ job_id: string; filename: string; size: number; warnings: string[] } | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [logs, setLogs] = useState("");
  const [target, setTarget] = useState<TargetFormat>("hydro");
  const [pidStart, setPidStart] = useState("P1000");
  const [owner, setOwner] = useState(1);
  const [tags, setTags] = useState("");
  const [only, setOnly] = useState("");
  const [runDoall, setRunDoall] = useState(false);
  const [missingEnv, setMissingEnv] = useState<MissingEnv>("warn");
  const [domjudgeCodeStart, setDomjudgeCodeStart] = useState("A");
  const [domjudgeColor, setDomjudgeColor] = useState("#000000");
  const [domjudgeWithStatement, setDomjudgeWithStatement] = useState(false);
  const [domjudgeWithAttachments, setDomjudgeWithAttachments] = useState(false);
  const [domjudgeAutoValidator, setDomjudgeAutoValidator] = useState(true);
  const [domjudgeDefaultValidator, setDomjudgeDefaultValidator] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRunning = job?.status === "queued" || job?.status === "running";
  const canStart = Boolean(inspect) && !isRunning && !busy && Boolean(token);
  const usesHydroOptions = target === "hydro";
  const usesDomjudgeOutputOptions = target === "domjudge" || target === "hydro_to_domjudge";
  const usesPolygonSource = target === "hydro" || target === "domjudge";
  const domjudgeColorPickerValue = /^#[0-9A-Fa-f]{6}$/.test(domjudgeColor) ? domjudgeColor : "#000000";

  const validation = useMemo(() => {
    if (target === "hydro") {
      if (!/^[A-Za-z]+[0-9]+$/.test(pidStart)) return "PID 起始值应类似 P1000。";
      if (!Number.isInteger(owner) || owner < 1) return "owner 必须是正整数。";
    }
    if (target === "domjudge" || target === "hydro_to_domjudge") {
      if (!/^[A-Za-z]+$/.test(domjudgeCodeStart)) return "DOMjudge 短名起始值应类似 A。";
      if (!/^#[0-9A-Fa-f]{6}$/.test(domjudgeColor)) return "DOMjudge 颜色必须是 #RRGGBB。";
    }
    if (target === "domjudge") {
      if (domjudgeAutoValidator && domjudgeDefaultValidator) return "自动识别 checker 和强制默认 validator 不能同时启用。";
    }
    return null;
  }, [domjudgeAutoValidator, domjudgeCodeStart, domjudgeColor, domjudgeDefaultValidator, owner, pidStart, target]);

  useEffect(() => {
    if (!token) return;
    getMe(token)
      .then(setUser)
      .catch(() => {
        window.localStorage.removeItem(tokenStorageKey);
        setToken("");
        setUser(null);
      });
  }, [token]);

  useEffect(() => {
    if (!token || !job || (job.status !== "queued" && job.status !== "running")) return;

    const timer = window.setInterval(async () => {
      try {
        const [nextJob, nextLogs] = await Promise.all([getJob(token, job.id), getLogs(token, job.id)]);
        setJob(nextJob);
        setLogs(nextLogs);
        if (nextJob.status !== "queued" && nextJob.status !== "running") {
          const nextUser = await getMe(token);
          setUser(nextUser);
          if (nextUser.is_admin) await refreshAdminUsers();
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "无法读取任务状态");
      }
    }, 1200);

    return () => window.clearInterval(timer);
  }, [job, token]);

  useEffect(() => {
    if (!token || !user?.is_admin) return;
    void refreshAdminUsers();
  }, [token, user?.is_admin]);

  async function refreshMe(nextToken = token) {
    if (!nextToken) return;
    setUser(await getMe(nextToken));
  }

  async function refreshAdminUsers() {
    if (!token) return;
    setAdminBusy(true);
    setAdminError(null);
    setAdminNotice(null);
    try {
      setAdminUsers(await listUsers(token));
    } catch (err) {
      setAdminError(err instanceof Error ? err.message : "无法读取用户列表");
    } finally {
      setAdminBusy(false);
    }
  }

  async function handleAuth(event: FormEvent) {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError(null);
    setAuthNotice(null);
    try {
      if (authMode === "register") {
        const response = await register(email, password);
        setEmail(response.email);
        setAuthMode("login");
        setAuthNotice("注册已提交，请等待管理员审核通过后再登录。");
        setPassword("");
        return;
      }

      const response = await login(email, password);
      window.localStorage.setItem(tokenStorageKey, response.token);
      setToken(response.token);
      setUser(response.user);
      setPassword("");
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "认证失败");
    } finally {
      setAuthBusy(false);
    }
  }

  function switchAuthMode(nextMode: AuthMode) {
    setAuthMode(nextMode);
    setAuthError(null);
    setAuthNotice(null);
  }

  function handleLogout() {
    window.localStorage.removeItem(tokenStorageKey);
    setToken("");
    setUser(null);
    setAdminUsers([]);
    setAdminPasswordDrafts({});
    setPasswordPanelOpen(false);
    setCurrentPassword("");
    setNewPassword("");
    setPasswordError(null);
    setPasswordNotice(null);
    setFile(null);
    setInspect(null);
    setJob(null);
    setLogs("");
    setError(null);
  }

  async function handleInspect() {
    if (!file || !token) return;
    setBusy(true);
    setError(null);
    setJob(null);
    setLogs("");
    try {
      setInspect(await inspectZip(token, file));
    } catch (err) {
      setInspect(null);
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleStart(event: FormEvent) {
    event.preventDefault();
    if (!inspect || validation || !token) return;
    setBusy(true);
    setError(null);
    try {
      const nextJob = await startJob(token, {
        job_id: inspect.job_id,
        target,
        pid_start: pidStart,
        owner,
        tags: splitList(tags),
        only: splitList(only),
        run_doall: usesPolygonSource && runDoall,
        missing_env: missingEnv,
        domjudge_code_start: domjudgeCodeStart,
        domjudge_color: domjudgeColor,
        domjudge_with_statement: domjudgeWithStatement,
        domjudge_with_attachments: domjudgeWithAttachments,
        domjudge_auto_validator: domjudgeAutoValidator,
        domjudge_default_validator: domjudgeDefaultValidator
      });
      setJob(nextJob);
      await refreshMe();
      if (user?.is_admin) await refreshAdminUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "任务启动失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (inspect && token) {
      try {
        await deleteJob(token, inspect.job_id);
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
    setTarget("hydro");
    setDomjudgeCodeStart("A");
    setDomjudgeColor("#000000");
    setDomjudgeWithStatement(false);
    setDomjudgeWithAttachments(false);
    setDomjudgeAutoValidator(true);
    setDomjudgeDefaultValidator(false);
  }

  async function handleDownload() {
    if (!job?.download_ready || !token) return;
    setError(null);
    try {
      const blob = await downloadJob(token, job.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `polygon-convert-${job.id}.zip`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "下载失败");
    }
  }

  function updateAdminDraft(userId: string, patch: Partial<UserResponse>) {
    setAdminUsers((items) => items.map((item) => (item.id === userId ? { ...item, ...patch } : item)));
  }

  function updateAdminPasswordDraft(userId: string, value: string) {
    setAdminPasswordDrafts((drafts) => ({ ...drafts, [userId]: value }));
  }

  async function handleChangePassword(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setPasswordBusy(true);
    setPasswordError(null);
    setPasswordNotice(null);
    try {
      await changePassword(token, currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setPasswordNotice("密码已更新。");
    } catch (err) {
      setPasswordError(err instanceof Error ? err.message : "密码修改失败");
    } finally {
      setPasswordBusy(false);
    }
  }

  async function saveAdminUser(item: UserResponse) {
    if (!token) return;
    setAdminBusy(true);
    setAdminError(null);
    setAdminNotice(null);
    try {
      const saved = await updateUser(token, item.id, {
        daily_quota: item.daily_quota,
        daily_used: item.daily_used,
        is_admin: item.is_admin,
        approval_status: item.approval_status
      });
      setAdminUsers((items) => items.map((current) => (current.id === saved.id ? saved : current)));
      if (saved.id === user?.id) setUser(saved);
      setAdminNotice("用户设置已保存。");
    } catch (err) {
      setAdminError(err instanceof Error ? err.message : "保存用户失败");
    } finally {
      setAdminBusy(false);
    }
  }

  async function handleResetUserPassword(item: UserResponse) {
    if (!token) return;
    const nextPassword = adminPasswordDrafts[item.id] || "";
    if (!nextPassword) return;
    setAdminBusy(true);
    setAdminError(null);
    setAdminNotice(null);
    try {
      await resetUserPassword(token, item.id, nextPassword);
      setAdminPasswordDrafts((drafts) => ({ ...drafts, [item.id]: "" }));
      setAdminNotice(`已重置 ${item.email} 的密码。`);
    } catch (err) {
      setAdminError(err instanceof Error ? err.message : "重置密码失败");
    } finally {
      setAdminBusy(false);
    }
  }

  if (!token || !user) {
    return (
      <main className="app-shell auth-shell">
        <section className="panel auth-panel">
          <div className="brand auth-brand">
            <Archive size={26} aria-hidden="true" />
            <div>
              <h1>Polygon Converter Web UI</h1>
              <p>账号通过管理员审核后可上传和转换题包</p>
            </div>
          </div>

          <form className="auth-form" onSubmit={handleAuth}>
            <div className="target-switch auth-switch" aria-label="认证模式">
              <button type="button" className={authMode === "login" ? "active" : ""} onClick={() => switchAuthMode("login")}>
                <strong>登录</strong>
                <small>已有账号</small>
              </button>
              <button type="button" className={authMode === "register" ? "active" : ""} onClick={() => switchAuthMode("register")}>
                <strong>注册</strong>
                <small>邮箱账号</small>
              </button>
            </div>

            <label>
              <span>邮箱</span>
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="email"
                required
              />
            </label>
            <label>
              <span>密码</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete={authMode === "login" ? "current-password" : "new-password"}
                minLength={8}
                required
              />
            </label>
            {authNotice && <div className="inline-notice">{authNotice}</div>}
            {authError && <div className="inline-error">{authError}</div>}
            <button className="primary-button wide" type="submit" disabled={authBusy}>
              <LogIn size={17} aria-hidden="true" />
              {authMode === "login" ? "邮箱登录" : "提交注册"}
            </button>
          </form>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <Archive size={24} aria-hidden="true" />
          <div>
            <h1>Polygon Converter Web UI</h1>
            <p>在受限 Docker runner 内转换 Polygon、HydroOJ 与 DOMjudge 题包</p>
          </div>
        </div>
        <div className="topbar-actions">
          <div className="user-chip">
            <Shield size={16} aria-hidden="true" />
            <span>{user.email}</span>
            <strong>{quotaText(user)}</strong>
          </div>
          <button className="ghost-button" type="button" onClick={() => setPasswordPanelOpen((open) => !open)}>
            <KeyRound size={17} aria-hidden="true" />
            修改密码
          </button>
          <button className="ghost-button" type="button" onClick={handleLogout}>
            <LogOut size={17} aria-hidden="true" />
            退出
          </button>
        </div>
      </header>

      {passwordPanelOpen && (
        <form className="panel account-panel" onSubmit={handleChangePassword}>
          <div className="panel-heading">
            <div>
              <h2>修改密码</h2>
              <p>使用当前密码确认身份后更新登录密码。</p>
            </div>
            <KeyRound size={20} aria-hidden="true" />
          </div>
          <div className="form-grid">
            <label>
              <span>当前密码</span>
              <input
                type="password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                autoComplete="current-password"
                minLength={8}
                required
              />
            </label>
            <label>
              <span>新密码</span>
              <input
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
                minLength={8}
                required
              />
            </label>
          </div>
          {passwordNotice && <div className="inline-notice">{passwordNotice}</div>}
          {passwordError && <div className="inline-error">{passwordError}</div>}
          <div className="button-row">
            <button className="primary-button" type="submit" disabled={passwordBusy}>
              <Save size={17} aria-hidden="true" />
              保存新密码
            </button>
            <button className="ghost-button" type="button" onClick={() => setPasswordPanelOpen(false)} disabled={passwordBusy}>
              关闭
            </button>
          </div>
        </form>
      )}

      <section className="workspace">
        <div className="left-column">
          <section className="panel upload-panel">
            <div className="panel-heading">
              <div>
                <h2>上传题包</h2>
                <p>接受 Polygon contest 或 HydroOJ zip，单次上传限制 128 MB。</p>
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
                <p>选择输入与输出格式后，只会显示该方向会使用的参数。</p>
              </div>
            </div>

            <div className="target-switch" aria-label="转换方向">
              {targetOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={target === option.value ? "active" : ""}
                  onClick={() => setTarget(option.value)}
                  disabled={isRunning}
                >
                  <strong>{option.label}</strong>
                  <small>{option.hint}</small>
                </button>
              ))}
            </div>

            {usesHydroOptions && (
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

                {target === "domjudge" && (
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
              </div>
            )}

            <label>
              <span>only</span>
              <input value={only} onChange={(event) => setOnly(event.target.value)} placeholder="a, b, buy-cpu" disabled={isRunning} />
            </label>

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
              <button className="download-button" type="button" onClick={handleDownload} disabled={!job?.download_ready}>
                <Download size={17} aria-hidden="true" />
                下载结果
              </button>
              <button className="ghost-button" type="button" onClick={handleReset} disabled={!inspect}>
                <Trash2 size={17} aria-hidden="true" />
                清理任务
              </button>
            </div>
          </section>

          <LogViewer logs={logs} />

          {user.is_admin && (
            <section className="panel admin-panel">
              <div className="panel-heading">
                <div>
                  <h2>用户配额</h2>
                  <p>管理员不受每日转换次数限制，可调整普通用户额度。</p>
                </div>
                <button className="ghost-button icon-button" type="button" onClick={refreshAdminUsers} disabled={adminBusy}>
                  <RefreshCw size={17} aria-hidden="true" />
                </button>
              </div>

              {adminError && <div className="inline-error">{adminError}</div>}
              {adminNotice && <div className="inline-notice">{adminNotice}</div>}

              <div className="user-table">
                {adminUsers.map((item) => (
                  <div className="user-row" key={item.id}>
                    <div className="user-row-main">
                      <Users size={17} aria-hidden="true" />
                      <div>
                        <strong>{item.email}</strong>
                        <span>
                          {item.is_admin
                            ? `${approvalStatusText(item.approval_status)} · 管理员`
                            : `${approvalStatusText(item.approval_status)} · 剩余 ${item.remaining_today ?? 0} 次 · 已用 ${item.daily_used} · 排队 ${item.daily_pending}`}
                        </span>
                      </div>
                    </div>
                    <label>
                      <span>审核状态</span>
                      <select
                        value={item.approval_status}
                        onChange={(event) => updateAdminDraft(item.id, { approval_status: event.target.value as UserResponse["approval_status"] })}
                      >
                        <option value="pending">待审核</option>
                        <option value="approved">已通过</option>
                        <option value="rejected">已拒绝</option>
                      </select>
                    </label>
                    <label>
                      <span>每日额度</span>
                      <input
                        type="number"
                        min={0}
                        value={item.daily_quota}
                        onChange={(event) => updateAdminDraft(item.id, { daily_quota: Number(event.target.value) })}
                      />
                    </label>
                    <label>
                      <span>今日已用</span>
                      <input
                        type="number"
                        min={0}
                        value={item.daily_used}
                        onChange={(event) => updateAdminDraft(item.id, { daily_used: Number(event.target.value) })}
                      />
                    </label>
                    <label className="checkbox-row admin-check">
                      <input type="checkbox" checked={item.is_admin} onChange={(event) => updateAdminDraft(item.id, { is_admin: event.target.checked })} />
                      <span>管理员</span>
                    </label>
                    <button className="primary-button" type="button" onClick={() => saveAdminUser(item)} disabled={adminBusy}>
                      <Save size={17} aria-hidden="true" />
                      保存
                    </button>
                    <div className="admin-reset-row">
                      <label>
                        <span>重置密码</span>
                        <input
                          type="password"
                          minLength={8}
                          value={adminPasswordDrafts[item.id] || ""}
                          onChange={(event) => updateAdminPasswordDraft(item.id, event.target.value)}
                          placeholder="至少 8 位"
                        />
                      </label>
                      <button
                        className="ghost-button"
                        type="button"
                        onClick={() => handleResetUserPassword(item)}
                        disabled={adminBusy || (adminPasswordDrafts[item.id] || "").length < 8}
                      >
                        <KeyRound size={17} aria-hidden="true" />
                        重置
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </section>
    </main>
  );
}
