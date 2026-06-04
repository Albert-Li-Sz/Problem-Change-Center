export type InspectResult = {
  job_id: string;
  filename: string;
  size: number;
  warnings: string[];
};

export type JobStatus = "queued" | "running" | "success" | "failed" | "cancelled";
export type TargetFormat = "hydro" | "domjudge" | "hydro_to_domjudge";

export type JobResponse = {
  id: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  download_ready: boolean;
  error: string | null;
};

export type JobRequest = {
  job_id: string;
  target: TargetFormat;
  pid_start: string;
  owner: number;
  tags: string[];
  only: string[];
  run_doall: boolean;
  missing_env: "warn" | "error";
  domjudge_code_start: string;
  domjudge_color: string;
  domjudge_with_statement: boolean;
  domjudge_with_attachments: boolean;
  domjudge_auto_validator: boolean;
  domjudge_default_validator: boolean;
};

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      throw new Error(body.detail || response.statusText);
    }
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
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
    throw new Error(await response.text());
  }
  return response.text();
}

export async function deleteJob(jobId: string): Promise<void> {
  const response = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export function downloadUrl(jobId: string): string {
  return `/api/jobs/${jobId}/download`;
}
