export type InspectResult = {
  job_id: string;
  filename: string;
  size: number;
  warnings: string[];
};

export type JobStatus = "queued" | "running" | "success" | "failed" | "cancelled";
export type TargetFormat = "hydro" | "domjudge" | "hydro_to_domjudge";
export type ApprovalStatus = "pending" | "approved" | "rejected";

export type JobResponse = {
  id: string;
  user_id: string | null;
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

export type UserResponse = {
  id: string;
  email: string;
  is_admin: boolean;
  approval_status: ApprovalStatus;
  daily_quota: number;
  daily_used: number;
  daily_pending: number;
  remaining_today: number | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
};

export type AuthResponse = {
  token: string;
  user: UserResponse;
};

export type RegisterResponse = {
  email: string;
  approval_status: ApprovalStatus;
};

export type AdminUpdateUserRequest = {
  daily_quota?: number;
  daily_used?: number;
  is_admin?: boolean;
  approval_status?: ApprovalStatus;
};

function authHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}` };
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText));
    }
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function register(email: string, password: string): Promise<RegisterResponse> {
  const response = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password })
  });
  return parseResponse<RegisterResponse>(response);
}

export async function login(email: string, password: string): Promise<AuthResponse> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password })
  });
  return parseResponse<AuthResponse>(response);
}

export async function changePassword(token: string, currentPassword: string, newPassword: string): Promise<void> {
  const response = await fetch("/api/auth/password", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText));
    }
    throw new Error(await response.text());
  }
}

export async function resetUserPassword(token: string, userId: string, newPassword: string): Promise<void> {
  const response = await fetch(`/api/admin/users/${userId}/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({ new_password: newPassword })
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText));
    }
    throw new Error(await response.text());
  }
}

export async function getMe(token: string): Promise<UserResponse> {
  const response = await fetch("/api/auth/me", {
    headers: authHeaders(token)
  });
  return parseResponse<UserResponse>(response);
}

export async function listUsers(token: string): Promise<UserResponse[]> {
  const response = await fetch("/api/admin/users", {
    headers: authHeaders(token)
  });
  return parseResponse<UserResponse[]>(response);
}

export async function updateUser(token: string, userId: string, payload: AdminUpdateUserRequest): Promise<UserResponse> {
  const response = await fetch(`/api/admin/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(payload)
  });
  return parseResponse<UserResponse>(response);
}

export async function inspectZip(token: string, file: File): Promise<InspectResult> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/inspect", {
    method: "POST",
    headers: authHeaders(token),
    body: form
  });
  return parseResponse<InspectResult>(response);
}

export async function startJob(token: string, payload: JobRequest): Promise<JobResponse> {
  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(payload)
  });
  return parseResponse<JobResponse>(response);
}

export async function getJob(token: string, jobId: string): Promise<JobResponse> {
  const response = await fetch(`/api/jobs/${jobId}`, {
    headers: authHeaders(token)
  });
  return parseResponse<JobResponse>(response);
}

export async function getLogs(token: string, jobId: string): Promise<string> {
  const response = await fetch(`/api/jobs/${jobId}/logs`, {
    headers: authHeaders(token)
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.text();
}

export async function deleteJob(token: string, jobId: string): Promise<void> {
  const response = await fetch(`/api/jobs/${jobId}`, {
    method: "DELETE",
    headers: authHeaders(token)
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function downloadJob(token: string, jobId: string): Promise<Blob> {
  const response = await fetch(`/api/jobs/${jobId}/download`, {
    headers: authHeaders(token)
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.blob();
}
