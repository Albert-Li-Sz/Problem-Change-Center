import { FormEvent, useEffect, useRef, useState } from "react";
import { Archive, Clock, Download, LogOut, Shield, UserRound } from "lucide-react";
import App from "../App";
import { apiFetch, cancelJob, deleteJob, downloadJob, JobResponse, onUnauthorized, parseResponse } from "../api";

type Quota = { daily_minutes: number; used: number; reserved: number; remaining: number };
type User = { id: string; email: string; is_admin: boolean; banned: boolean; verified: boolean; daily_minutes: number; quota: Quota };
type Config = { turnstile_site_key: string; development: boolean; contact_email: string; registration_open: boolean };
type Status = { registration_open: boolean; queue_open: boolean; worker: { heartbeat_at: string; active: number; disk_percent: number } | null };
type Invitation = { id: string; expires_at: string; used_at: string | null; revoked: boolean };
type AdminJob = { id: string; status: string; kind: string; user_id: string };
type Screen = "convert" | "history" | "account" | "admin" | "terms";
type AuthMode = "login" | "register" | "forgot" | "resend" | "verify" | "reset";

declare global {
  interface Window {
    turnstile?: {
      render: (element: HTMLElement, options: Record<string, unknown>) => string;
      remove: (id: string) => void;
    };
  }
}

async function call<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  return parseResponse<T>(await apiFetch(path, {
    method, headers: data === undefined ? undefined : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data)
  }));
}

function Captcha({ siteKey, onToken }: { siteKey: string; onToken: (token: string) => void }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let widget: string | undefined;
    const install = () => {
      if (container.current && window.turnstile && widget === undefined) {
        widget = window.turnstile.render(container.current, { sitekey: siteKey, callback: onToken, "expired-callback": () => onToken("") });
      }
    };
    let script = document.querySelector<HTMLScriptElement>("script[data-turnstile]");
    if (!script) {
      script = document.createElement("script");
      script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      script.dataset.turnstile = "true";
      script.async = true;
      document.head.append(script);
    }
    script.addEventListener("load", install);
    install();
    return () => {
      script?.removeEventListener("load", install);
      if (widget && window.turnstile) window.turnstile.remove(widget);
    };
  }, [siteKey, onToken]);
  return <div ref={container} className="public-captcha" />;
}

function Terms({ contact }: { contact: string }) {
  return <article className="public-card public-terms">
    <h2>服务条款与隐私说明</h2>
    <p>本站是免费公益题包转换工具，采用邀请码开放使用。额度用于公平分配计算资源，不涉及收费。</p>
    <p>仅上传您有权处理的题包。不得利用本站攻击系统、绕过配额、挖矿或传播违法内容。请自行核验转换结果，本站不承诺永久保存文件。</p>
    <p>题包默认仅您本人可见，不公开分享。上传文件、转换结果、报告与日志在任务完成后保留 24 小时；未开始的上传自创建起保留 24 小时。用户可提前删除任务或注销账号。</p>
    <p>账号保存邮箱与密码哈希，登录 Cookie 用于维持会话。IP 的不可逆摘要用于限流；安全审计保留最多 30 天。数据库加密备份保留 7 天，删除记录可能在备份到期前仍存在。</p>
    <p>邮件服务负责发送验证与找回密码邮件；Cloudflare Turnstile 用于人机验证。除安全排障和处理举报外，运营方不主动查看题包内容。</p>
    <p>隐私、内容权利或滥用问题请联系：{contact ? <a href={`mailto:${contact}`}>{contact}</a> : "站点管理员"}。</p>
  </article>;
}

const labels: Record<string, string> = { uploading: "上传中", uploaded: "检查完成", queued: "排队中", running: "运行中", success: "已完成", failed: "失败", cancelled: "已取消" };

export function PublicPlatform() {
  const [config, setConfig] = useState<Config | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<AuthMode>("login");
  const [token, setToken] = useState("");
  const [captcha, setCaptcha] = useState("");
  const [captchaKey, setCaptchaKey] = useState(0);
  const [screen, setScreen] = useState<Screen>("convert");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<JobResponse[]>([]);
  const [selectedJob, setSelectedJob] = useState<string>();
  const [users, setUsers] = useState<User[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [codes, setCodes] = useState<string[]>([]);
  const [adminJobs, setAdminJobs] = useState<AdminJob[]>([]);
  const [status, setStatus] = useState<Status | null>(null);

  const refreshMe = () => call<User>("/api/account").then(setUser);
  const refreshHistory = () => call<{ items: JobResponse[]; quota: Quota }>("/api/jobs").then((value) => {
    setHistory(value.items);
    setUser((current) => current ? { ...current, quota: value.quota } : current);
  });
  const refreshAdmin = () => Promise.all([
    call<User[]>("/api/admin/users"), call<Invitation[]>("/api/admin/invitations"),
    call<AdminJob[]>("/api/admin/jobs"), call<Status>("/api/admin/status")
  ]).then(([nextUsers, nextInvites, nextJobs, nextStatus]) => {
    setUsers(nextUsers); setInvitations(nextInvites); setAdminJobs(nextJobs); setStatus(nextStatus);
  });

  useEffect(() => {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    for (const action of ["verify", "reset"] as const) {
      const value = hash.get(action);
      if (value) { setToken(value); setMode(action); window.history.replaceState(null, "", window.location.pathname); }
    }
    void Promise.all([
      call<Config>("/api/public/config").then(setConfig),
      refreshMe().catch(() => setUser(null))
    ]).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false));
    return onUnauthorized(() => setUser(null));
  }, []);

  useEffect(() => {
    if (!user) return;
    const refresh = screen === "history" ? refreshHistory : screen === "admin" && user.is_admin ? refreshAdmin : refreshMe;
    void refresh().catch((reason: Error) => setError(reason.message));
    const interval = window.setInterval(() => { void refresh().catch(() => undefined); }, 10000);
    return () => window.clearInterval(interval);
  }, [screen, user?.id]);

  async function action(work: () => Promise<unknown>, success = "") {
    setBusy(true); setError(""); setMessage("");
    try { await work(); if (success) setMessage(success); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }

  async function authenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await action(async () => {
      const body: Record<string, unknown> = { email: form.get("email"), password: form.get("password"), captcha, token };
      if (mode === "register") { body.invitation = form.get("invitation"); body.accept_terms = form.get("terms") === "on"; }
      const result = await call<{ message?: string }>(`/api/auth/${mode}`, "POST", body);
      if (mode === "login") { await refreshMe(); setScreen("convert"); }
      else { setMode("login"); setToken(""); }
      setMessage(result.message ?? "");
    });
    setCaptcha(""); setCaptchaKey((key) => key + 1);
  }

  const changeMode = (next: AuthMode) => { setMode(next); setError(""); setMessage(""); setCaptcha(""); setCaptchaKey((key) => key + 1); };
  const needsCaptcha = ["login", "register", "forgot", "resend"].includes(mode);
  const title = { login: "欢迎回来", register: "加入公益站", forgot: "找回密码", resend: "重新发送验证邮件", verify: "验证邮箱", reset: "设置新密码" }[mode];

  return <div className="public-platform">
    <header className="public-header"><a href="/" className="public-brand"><Archive size={22} />题包转换公益站 <span>邀请 Beta</span></a>
      {user && <div className="public-identity"><span>{user.email}</span><button onClick={() => void action(async () => { await call("/api/auth/logout", "POST"); setUser(null); })}><LogOut size={15} />退出</button></div>}
    </header>
    {loading ? <main className="public-card">正在连接站点…</main> : <>
      {(error || message) && <div role={error ? "alert" : "status"} className={`public-notice ${error ? "error" : ""}`}>{error || message}</div>}
      {screen === "terms" ? <><Terms contact={config?.contact_email ?? ""} /><button className="public-back" onClick={() => setScreen("convert")}>返回</button></> : !user || mode === "verify" || mode === "reset" ?
        <main className="public-auth"><section className="public-intro"><span className="public-eyebrow">FREE · COMMUNITY</span><h1>让题包<br />自由转换。</h1><p>连接 Polygon、Hydro、DOMjudge 等 OJ 格式。<br />上传、转换、下载，保留你的出题工作流。</p><div className="public-points"><span><Shield size={18} />题包仅自己可见</span><span><Clock size={18} />文件保留 24 小时</span><span><Archive size={18} />完全免费</span></div></section>
          <section className="public-card public-login"><h2>{title}</h2><p>{mode === "register" ? "准备好邀请码，验证邮箱后即可使用。" : "使用邮箱登录，继续你的转换任务。"}</p>
            <form onSubmit={(event) => void authenticate(event)}>
              {!["verify", "reset"].includes(mode) && <label>邮箱<input name="email" type="email" autoComplete="email" required maxLength={254} /></label>}
              {["login", "register", "reset"].includes(mode) && <label>密码<input name="password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required minLength={mode === "login" ? 1 : 12} maxLength={128} /><small>{mode !== "login" && "至少 12 个字符"}</small></label>}
              {mode === "register" && <><label>邀请码<input name="invitation" required maxLength={128} autoComplete="off" /></label><label className="public-checkbox"><input name="terms" type="checkbox" required />我已阅读并同意<button type="button" className="public-link" onClick={() => setScreen("terms")}>服务条款与隐私说明</button></label></>}
              {needsCaptcha && config && !config.development && <Captcha key={captchaKey} siteKey={config.turnstile_site_key} onToken={setCaptcha} />}
              <button className="public-primary" disabled={busy || !config || (needsCaptcha && !config.development && !captcha) || (mode === "register" && !config.registration_open)}>{busy ? "处理中…" : title}</button>
            </form>
            <div className="public-auth-links"><button onClick={() => changeMode(mode === "register" ? "login" : "register")}>{mode === "register" ? "已有账号，登录" : "使用邀请码注册"}</button><button onClick={() => changeMode("forgot")}>忘记密码</button><button onClick={() => changeMode("resend")}>重发验证邮件</button>{mode !== "login" && <button onClick={() => changeMode("login")}>返回登录</button>}</div>
          </section></main>
        : <>
          <nav className="public-nav" aria-label="平台导航">{([["convert", "新建转换"], ["history", "任务历史"], ["account", "账号设置"], ...(user.is_admin ? [["admin", "管理后台"]] : [])] as [Screen, string][]).map(([key, text]) => <button className={screen === key ? "selected" : ""} key={key} onClick={() => { setScreen(key); if (key === "convert") setSelectedJob(undefined); }}>{text}</button>)}<span className="public-quota">今日剩余 <strong>{user.quota?.remaining ?? "–"}</strong> 分钟 · 已预留 {user.quota?.reserved ?? 0} 分钟</span></nav>
          {screen === "convert" && <App key={selectedJob ?? "new"} initialJobId={selectedJob} />}
          {screen === "history" && <main className="public-card public-wide"><div className="public-section-title"><h2>任务历史</h2><button onClick={() => void action(refreshHistory)}>刷新</button></div><p>检查与转换都进入队列。文件自动保留 24 小时，额度每日 UTC 00:00 重置。</p>{!history.length && <div className="public-empty"><Archive size={32} /><p>还没有任务，创建一次转换开始吧。</p></div>}
            <div className="public-job-list">{history.map((job) => <article key={job.id} className="public-job"><div><strong>{job.filename ?? job.id}</strong><p>{labels[job.lifecycle ?? job.status]}{job.kind === "inspect" ? " · 题包检查" : ""}{job.queue_position ? ` · 前方约 ${job.queue_position - 1} 个任务` : ""}</p><small>{job.expires_at && `过期：${new Date(job.expires_at).toLocaleString()}`} · 已用 {job.billed_minutes ?? 0} 分钟</small>{job.error && <p className="public-error-text">{job.error}</p>}</div><div className="public-actions"><button disabled={job.kind === "inspect" && ["queued", "running"].includes(job.lifecycle ?? "")} onClick={() => { setSelectedJob(job.id); setScreen("convert"); }}>查看 / 继续</button>{job.download_ready && <button onClick={() => void action(() => downloadJob(job.id))}><Download size={14} />下载</button>}{["queued", "running"].includes(job.lifecycle ?? "") && <button onClick={() => void action(async () => { await cancelJob(job.id); await refreshHistory(); })}>取消</button>}<button onClick={() => void action(async () => { await deleteJob(job.id); await refreshHistory(); })}>删除</button></div></article>)}</div>
          </main>}
          {screen === "account" && <main className="public-card public-account"><h2><UserRound size={20} />账号设置</h2><p>{user.email} · 每日 {user.daily_minutes} 分钟 · 每人最多运行 2 个、排队 5 个任务</p><h3>修改密码</h3><form onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void action(async () => { await call("/api/auth/password", "POST", Object.fromEntries(data)); setUser(null); }, "密码已修改，请重新登录"); }}><label>当前密码<input name="current_password" type="password" required autoComplete="current-password" /></label><label>新密码<input name="new_password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" /></label><button disabled={busy}>修改密码并退出所有设备</button></form><hr /><h3>注销账号</h3><p>注销后立即停止访问，并删除所有任务文件。此操作无法撤销。</p><form onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void action(async () => { await call("/api/account", "DELETE", { password: data.get("password") }); setUser(null); }, "账号已注销，任务文件正在清理"); }}><label>输入密码确认<input name="password" type="password" required autoComplete="current-password" /></label><label className="public-checkbox"><input type="checkbox" required />确认注销账号并删除全部任务</label><button disabled={busy} className="public-danger">注销并删除资料</button></form></main>}
          {screen === "admin" && user.is_admin && <main className="public-admin public-wide"><section className="public-card"><h2>站点状态</h2><p>Worker：{status?.worker ? `${status.worker.active} 个运行任务 · 磁盘 ${status.worker.disk_percent}% · 心跳 ${new Date(status.worker.heartbeat_at).toLocaleTimeString()}` : "等待上线"}</p>{status && <div className="public-actions"><button onClick={() => void action(async () => { await call("/api/admin/settings", "PATCH", { registration_open: !status.registration_open, queue_open: status.queue_open }); await refreshAdmin(); })}>{status.registration_open ? "暂停注册" : "恢复注册"}</button><button onClick={() => void action(async () => { await call("/api/admin/settings", "PATCH", { registration_open: status.registration_open, queue_open: !status.queue_open }); await refreshAdmin(); })}>{status.queue_open ? "暂停领取任务" : "恢复任务队列"}</button></div>}</section>
            <section className="public-card"><h2>邀请码</h2><button onClick={() => void action(async () => { const result = await call<{ codes: string[] }>("/api/admin/invitations", "POST", { count: 10 }); setCodes(result.codes); await refreshAdmin(); })}>生成 10 个一次性邀请码</button>{codes.length > 0 && <><p>明文只显示这一次，请及时保存。有效期 7 天。</p><textarea aria-label="生成的邀请码" rows={10} readOnly value={codes.join("\n")} /><button onClick={() => { const link = document.createElement("a"); const url = URL.createObjectURL(new Blob([codes.join("\n")], { type: "text/plain" })); link.href = url; link.download = "invitations.txt"; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }}>导出邀请码</button></>}<div className="public-table-wrap"><table><thead><tr><th>标识</th><th>状态</th><th>到期时间</th><th>操作</th></tr></thead><tbody>{invitations.map((invite) => <tr key={invite.id}><td>{invite.id.slice(0, 8)}</td><td>{invite.revoked ? "已撤销" : invite.used_at ? "已使用" : "未使用"}</td><td>{new Date(invite.expires_at).toLocaleString()}</td><td><button disabled={invite.revoked || !!invite.used_at} onClick={() => void action(async () => { await call(`/api/admin/invitations/${invite.id}`, "DELETE"); await refreshAdmin(); })}>撤销</button></td></tr>)}</tbody></table></div></section>
            <section className="public-card"><h2>用户与额度</h2>{users.map((person) => <form className="public-user-row" key={person.id} onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void action(async () => { await call(`/api/admin/users/${person.id}`, "PATCH", { daily_minutes: Number(data.get("minutes")) }); await refreshAdmin(); }, "额度已更新"); }}><span>{person.email}<small>{person.is_admin ? "管理员" : person.banned ? "已封禁" : person.verified ? "已验证" : "待验证"}</small></span><label>每日分钟<input aria-label={`${person.email} 每日额度`} type="number" name="minutes" min={1} max={1440} defaultValue={person.daily_minutes} required /></label><button>保存</button><button type="button" disabled={person.is_admin} onClick={() => void action(async () => { await call(`/api/admin/users/${person.id}`, "PATCH", { banned: !person.banned }); await refreshAdmin(); })}>{person.banned ? "解除封禁" : "封禁"}</button></form>)}</section>
            <section className="public-card"><h2>全站任务</h2>{adminJobs.map((job) => <div className="public-job" key={job.id}><span>{job.id.slice(0, 12)} · {job.kind} · {labels[job.status]}</span><button disabled={!["running", "queued"].includes(job.status)} onClick={() => void action(async () => { await call(`/api/admin/jobs/${job.id}/cancel`, "POST"); await refreshAdmin(); })}>终止</button></div>)}</section>
          </main>}
        </>}
      <footer className="public-footer"><span>免费公益 · 有限资源，公平使用</span><button onClick={() => setScreen("terms")}>服务条款与隐私说明</button>{config?.contact_email && <a href={`mailto:${config.contact_email}`}>联系 / 举报</a>}</footer>
    </>}
  </div>;
}
