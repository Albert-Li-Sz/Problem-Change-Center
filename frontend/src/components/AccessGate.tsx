import { Archive, KeyRound, LoaderCircle, LockKeyhole, RefreshCw } from "lucide-react";
import { FormEvent, ReactNode, useEffect, useState } from "react";

import {
  accessConfiguration,
  clearAccessKey,
  getStoredAccessKey,
  onUnauthorized,
  storeAccessKey,
  verifyAccessKey
} from "../api";

type AccessGateProps = {
  children: ReactNode;
};

type GateState = "checking" | "locked" | "ready";

export function AccessGate({ children }: AccessGateProps) {
  const [gateState, setGateState] = useState<GateState>("checking");
  const [external, setExternal] = useState(false);
  const [accessKey, setAccessKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function checkDeployment() {
    setGateState("checking");
    setError(null);
    try {
      const configuration = await accessConfiguration();
      setExternal(configuration.accessKeyRequired);
      if (!configuration.accessKeyRequired) {
        clearAccessKey();
        setGateState("ready");
        return;
      }
      const stored = getStoredAccessKey();
      if (stored && (await verifyAccessKey(stored))) {
        setGateState("ready");
        return;
      }
      clearAccessKey();
      setGateState("locked");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法连接服务");
      setGateState("locked");
    }
  }

  useEffect(() => {
    void checkDeployment();
    return onUnauthorized(() => {
      setExternal(true);
      setAccessKey("");
      setError("访问密钥已失效，请重新验证。");
      setGateState("locked");
    });
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!accessKey || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      if (!(await verifyAccessKey(accessKey))) {
        setError("访问密钥不正确。");
        return;
      }
      storeAccessKey(accessKey);
      setAccessKey("");
      setGateState("ready");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "验证失败");
    } finally {
      setSubmitting(false);
    }
  }

  function lockWorkspace() {
    clearAccessKey();
    setAccessKey("");
    setError(null);
    setGateState("locked");
  }

  if (gateState === "ready") {
    return (
      <>
        {external ? (
          <div className="access-session-bar">
            <span>
              <LockKeyhole size={15} aria-hidden="true" />
              外部访问已验证
            </span>
            <button type="button" onClick={lockWorkspace}>
              锁定
            </button>
          </div>
        ) : null}
        {children}
      </>
    );
  }

  return (
    <main className="access-gate-shell">
      <header className="access-gate-brand">
        <span>
          <Archive size={19} aria-hidden="true" />
        </span>
        Problem Change Center
      </header>
      <section className="access-gate-panel" aria-labelledby="access-gate-title">
        <div className="access-gate-icon" aria-hidden="true">
          {gateState === "checking" ? (
            <LoaderCircle className="spin" size={24} />
          ) : (
            <KeyRound size={24} />
          )}
        </div>
        <h1 id="access-gate-title">
          {gateState === "checking" ? "正在连接" : "访问验证"}
        </h1>
        {gateState === "locked" ? (
          <form onSubmit={handleSubmit}>
            <label>
              <span>访问密钥</span>
              <input
                type="password"
                value={accessKey}
                onChange={(event) => setAccessKey(event.target.value)}
                autoComplete="current-password"
                autoFocus
                disabled={submitting}
              />
            </label>
            {error ? (
              <div className="access-gate-error" role="alert">
                {error}
              </div>
            ) : null}
            <button
              className="primary-button"
              type="submit"
              disabled={!accessKey || submitting}
            >
              <KeyRound size={17} aria-hidden="true" />
              {submitting ? "验证中" : "进入工作台"}
            </button>
            {!external && error ? (
              <button
                className="ghost-button"
                type="button"
                onClick={() => void checkDeployment()}
              >
                <RefreshCw size={17} aria-hidden="true" />
                重试连接
              </button>
            ) : null}
          </form>
        ) : null}
      </section>
    </main>
  );
}
