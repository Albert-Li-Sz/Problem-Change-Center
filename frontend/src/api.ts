export type InspectResult = {
  job_id: string;
  filename: string;
  size: number;
  warnings: string[];
  detected_format: FormatId | null;
  format_candidates: Array<{ format: FormatId; confidence: number; evidence: string[] }>;
};

export type JobStatus = "queued" | "running" | "success" | "failed" | "cancelled";
export type FormatId =
  | "polygon"
  | "hydro"
  | "icpc"
  | "hoj"
  | "fps"
  | "qduoj"
  | "uoj"
  | "dmoj"
  | "generic";
export type SourceFormat = "auto" | FormatId;
export type TargetFormat = Exclude<FormatId, "polygon" | "generic">;

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
  onLogs: (logs: string) => void;
  onReport: (report: ConversionReport) => void;
  onError: () => void;
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
  const response = await fetch("/api/inspect", {
    method: "POST",
    body: form
  });
  return parseResponse<InspectResult>(response);
}

export async function startJob(payload: JobRequest): Promise<JobResponse> {
  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  return parseResponse<JobResponse>(response);
}

export async function getJob(jobId: string): Promise<JobResponse> {
  const response = await fetch(`/api/jobs/${jobId}`);
  return parseResponse<JobResponse>(response);
}

export async function getLogs(jobId: string): Promise<string> {
  const response = await fetch(`/api/jobs/${jobId}/logs`);
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
  return response.text();
}

export async function getReport(jobId: string): Promise<ConversionReport> {
  const response = await fetch(`/api/jobs/${jobId}/report`);
  return parseResponse<ConversionReport>(response);
}

export function subscribeToJobEvents(
  jobId: string,
  handlers: JobEventHandlers
): (() => void) | null {
  if (typeof EventSource === "undefined") return null;
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  const parse = <T>(event: Event, handler: (value: T) => void) => {
    try {
      handler(JSON.parse((event as MessageEvent<string>).data) as T);
    } catch {
      handlers.onError();
    }
  };
  source.onopen = handlers.onOpen;
  source.addEventListener("job", (event) =>
    parse<JobResponse>(event, handlers.onJob)
  );
  source.addEventListener("logs", (event) =>
    parse<{ text: string }>(event, ({ text }) => handlers.onLogs(text))
  );
  source.addEventListener("report", (event) =>
    parse<ConversionReport>(event, handlers.onReport)
  );
  source.onerror = handlers.onError;
  return () => source.close();
}

export async function deleteJob(jobId: string): Promise<void> {
  const response = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }
}

export async function cancelJob(jobId: string): Promise<void> {
  const response = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
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
  const response = await fetch(`/api/jobs/${jobId}/repairs`, {
    method: "POST",
    body: form
  });
  return parseResponse<JobResponse>(response);
}

export function downloadUrl(jobId: string): string {
  return `/api/jobs/${jobId}/download`;
}
