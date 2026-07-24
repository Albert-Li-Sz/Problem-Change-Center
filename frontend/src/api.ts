import { fetchEventSource } from "@microsoft/fetch-event-source";

const accessKeyStorageName = "p2h.access-key";
const unauthorizedEventName = "p2h:unauthorized";

function apiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim();
  if (configured) return configured.replace(/\/$/, "");
  if (typeof window !== "undefined" && window.location.port === "11452") {
    const backend = new URL(window.location.href);
    backend.port = "11451";
    backend.pathname = "/";
    backend.search = "";
    backend.hash = "";
    return backend.origin;
  }
  return "";
}

function apiUrl(path: string): string {
  return `${apiBaseUrl()}${path}`;
}

export function getStoredAccessKey(): string {
  if (typeof window === "undefined") return "";
  return window.sessionStorage.getItem(accessKeyStorageName) ?? "";
}

export function storeAccessKey(accessKey: string): void {
  window.sessionStorage.setItem(accessKeyStorageName, accessKey);
}

export function clearAccessKey(): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(accessKeyStorageName);
}

function notifyUnauthorized(): void {
  clearAccessKey();
  window.dispatchEvent(new Event(unauthorizedEventName));
}

export function onUnauthorized(listener: () => void): () => void {
  window.addEventListener(unauthorizedEventName, listener);
  return () => window.removeEventListener(unauthorizedEventName, listener);
}

function accessHeaders(
  initial?: HeadersInit,
  accessKey = getStoredAccessKey()
): Headers {
  const headers = new Headers(initial);
  if (accessKey) headers.set("X-P2H-Access-Key", accessKey);
  return headers;
}

async function apiFetch(
  path: string,
  init: RequestInit = {},
  accessKey = getStoredAccessKey(),
  notifyOnUnauthorized = true
): Promise<Response> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: accessHeaders(init.headers, accessKey)
  });
  if (response.status === 401 && notifyOnUnauthorized) notifyUnauthorized();
  return response;
}

export type InspectResult = {
  job_id: string;
  filename: string;
  size: number;
  warnings: string[];
  detected_format: FormatId | null;
  format_candidates: Array<{ format: FormatId; confidence: number; evidence: string[] }>;
  package_scope: "single" | "multi" | "unknown";
  package_layout: "directory" | "contest" | "workspace" | "nested" | "xml" | "mixed" | "unknown";
  problem_count: number | null;
  problems: Array<{ id: string; path: string }>;
  problems_truncated: boolean;
  supported_targets: TargetFormat[];
};

export type JobStatus = "queued" | "running" | "success" | "failed" | "cancelled";
export type FormatId =
  | "polygon"
  | "probhub"
  | "hydro"
  | "icpc"
  | "hoj"
  | "fps"
  | "qduoj"
  | "uoj"
  | "dmoj"
  | "generic";
export type SourceFormat = "auto" | FormatId;
export type TargetFormat = Exclude<
  FormatId,
  "polygon" | "probhub" | "generic"
>;

export type ConversionIssue = {
  severity: "warning" | "loss" | "fatal";
  code: string;
  message: string;
  problem: string | null;
  field: string | null;
  context?: Record<string, string>;
};

export type RepairCandidate = {
  path: string;
  strategy: "case-only" | "extension-alias" | "unique-basename";
  confidence: number;
};

export type RepairSuggestion = {
  id: string;
  issue_code: string;
  expected_path: string;
  role: string;
  problem: string | null;
  candidates: RepairCandidate[];
  requires_upload: boolean;
};

export type ConversionReport = {
  schema_version: number;
  source_format: string;
  target_format: string;
  problem_count: number;
  counts: { warning: number; loss: number; fatal: number };
  issues: ConversionIssue[];
  artifacts: string[];
  repair_ready?: boolean;
  repair_suggestions?: RepairSuggestion[];
  applied_repairs?: Array<Record<string, string>>;
  source_semantic_digest?: string | null;
};

export type JobResponse = {
  id: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  download_ready: boolean;
  error: string | null;
  source_format: string | null;
  target_format: string | null;
  report_ready: boolean;
  report_counts: { warning: number; loss: number; fatal: number };
  progress?: {
    phase: "validate_archive" | "extract" | "detect" | "read" | "validate_ir" | "write" | "validate_output" | "package";
    current: number | null;
    total: number | null;
    unit: string | null;
    problem: string | null;
    detail: string | null;
    started_at: string;
    last_activity_at: string;
  } | null;
  timeout?: {
    kind: "overall" | "idle" | "stage" | "problem";
    limit_seconds: number;
    phase: string | null;
    problem: string | null;
  } | null;
};

export type JobRequest = {
  job_id: string;
  source_format: SourceFormat;
  target_format: TargetFormat;
  loss_policy: "warn" | "error";
  only: string[];
  options: {
    polygon: {
      run_doall: boolean;
      missing_env: "warn" | "error";
      with_statement: boolean;
      with_attachments: boolean;
      validator_mode: "auto" | "default" | "custom";
    };
    hydro: { pid_start: string; owner: number; tags: string[] };
    icpc: {
      code_start: string;
      color: string;
      profile: "legacy-icpc" | "2025-09";
      license: "unknown" | "public domain" | "cc0" | "cc by" | "cc by-sa" | "educational" | "permission";
      rights_owner: string;
    };
    fps: { profile: "hustoj-1.6" | "qduoj-1.2" };
  };
};

export type JobEventHandlers = {
  onOpen: () => void;
  onJob: (job: JobResponse) => void;
  onLogs: (chunk: LogChunk) => void;
  onReport: (report: ConversionReport) => void;
  onCursor?: (cursor: string) => void;
  onError: () => void;
};

export type LogChunk = {
  text: string;
  offset: number;
  next_offset: number;
  reset: boolean;
};

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return response.json() as Promise<T>;
}

function formatErrorDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== "object") return String(item);
        const record = item as Record<string, unknown>;
        const location = Array.isArray(record.loc)
          ? record.loc.map(String).filter((part) => part !== "body").join(".")
          : "";
        const message = typeof record.msg === "string" ? record.msg : JSON.stringify(item);
        return location ? `${location}: ${message}` : message;
      })
      .filter(Boolean);
    return messages.length ? messages.join("；") : null;
  }
  if (detail && typeof detail === "object") {
    try {
      return JSON.stringify(detail);
    } catch {
      return null;
    }
  }
  return detail == null ? null : String(detail);
}

async function responseErrorMessage(response: Response): Promise<string> {
  const fallback = response.statusText || `HTTP ${response.status}`;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const body = (await response.json()) as { detail?: unknown };
      return formatErrorDetail(body.detail) || fallback;
    } catch {
      return fallback;
    }
  }
  try {
    return (await response.text()).trim() || fallback;
  } catch {
    return fallback;
  }
}

export async function inspectZip(file: File): Promise<InspectResult> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiFetch("/api/inspect", {
    method: "POST",
    body: form
  });
  return parseResponse<InspectResult>(response);
}

export async function startJob(payload: JobRequest): Promise<JobResponse> {
  const response = await apiFetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  return parseResponse<JobResponse>(response);
}

export async function getJob(jobId: string): Promise<JobResponse> {
  const response = await apiFetch(`/api/jobs/${jobId}`);
  return parseResponse<JobResponse>(response);
}

export async function getLogs(jobId: string): Promise<string> {
  const response = await apiFetch(`/api/jobs/${jobId}/logs`);
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return response.text();
}

export async function getReport(jobId: string): Promise<ConversionReport> {
  const response = await apiFetch(`/api/jobs/${jobId}/report`);
  return parseResponse<ConversionReport>(response);
}

export function subscribeToJobEvents(
  jobId: string,
  handlers: JobEventHandlers,
  cursor = ""
): (() => void) | null {
  if (typeof AbortController === "undefined") return null;
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const controller = new AbortController();
  const parse = <T>(data: string, handler: (value: T) => void) => {
    try {
      handler(JSON.parse(data) as T);
    } catch {
      handlers.onError();
    }
  };

  void fetchEventSource(apiUrl(`/api/jobs/${jobId}/events${query}`), {
    headers: Object.fromEntries(
      accessHeaders({ Accept: "text/event-stream" }).entries()
    ),
    signal: controller.signal,
    openWhenHidden: true,
    async onopen(response) {
      if (response.status === 401) {
        notifyUnauthorized();
        throw new Error("Invalid or missing access key");
      }
      if (!response.ok) {
        throw new Error(await responseErrorMessage(response));
      }
      handlers.onOpen();
    },
    onmessage(message) {
      if (message.id) handlers.onCursor?.(message.id);
      if (message.event === "job") {
        parse<JobResponse>(message.data, handlers.onJob);
      } else if (message.event === "logs") {
        parse<LogChunk>(message.data, handlers.onLogs);
      } else if (message.event === "report") {
        parse<ConversionReport>(message.data, handlers.onReport);
      }
    },
    onclose() {
      if (!controller.signal.aborted) handlers.onError();
    },
    onerror(error) {
      throw error;
    }
  }).catch(() => {
    if (!controller.signal.aborted) handlers.onError();
  });

  return () => controller.abort();
}

export async function deleteJob(jobId: string): Promise<void> {
  const response = await apiFetch(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
}

export async function cancelJob(jobId: string): Promise<void> {
  const response = await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
}

export async function applyRepairs(
  jobId: string,
  selections: Array<{
    suggestion_id: string;
    candidate_path?: string;
    upload_name?: string;
  }>,
  files: File[]
): Promise<JobResponse> {
  const form = new FormData();
  form.append("plan", JSON.stringify({ selections }));
  for (const file of files) form.append("files", file, file.name);
  const response = await apiFetch(`/api/jobs/${jobId}/repairs`, {
    method: "POST",
    body: form
  });
  return parseResponse<JobResponse>(response);
}

function downloadFilename(response: Response, jobId: string): string {
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
  let filename = `oj-package-convert-${jobId}.zip`;
  try {
    filename = encoded ? decodeURIComponent(encoded) : quoted || filename;
  } catch {
    filename = quoted || filename;
  }
  return filename.replace(/[\\/\0]/g, "_");
}

export async function downloadJob(jobId: string): Promise<void> {
  const response = await apiFetch(`/api/jobs/${jobId}/download`);
  if (!response.ok) throw new Error(await responseErrorMessage(response));
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = downloadFilename(response, jobId);
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export async function accessConfiguration(): Promise<{
  accessKeyRequired: boolean;
}> {
  const response = await fetch(apiUrl("/api/health/live"), {
    headers: { Accept: "application/json" }
  });
  const payload = await parseResponse<{
    status: string;
    access_key_required?: boolean;
  }>(response);
  return { accessKeyRequired: payload.access_key_required === true };
}

export async function verifyAccessKey(accessKey: string): Promise<boolean> {
  const response = await apiFetch(
    "/api/health",
    { headers: { Accept: "application/json" } },
    accessKey,
    false
  );
  if (response.status === 401) return false;
  await parseResponse<{ status: string }>(response);
  return true;
}
