import {
  ChatResponse,
  ConnectionCreateRequest,
  ConnectionCreateResponse,
  ConnectionProfile,
  ConnectionTestRequest,
  ConnectionTestResponse,
  GenerateResponse,
  JobStatusResponse,
  SchemaResponse,
  User,
} from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  (typeof window !== "undefined"
    ? `${window.location.origin}/api`
    : "http://localhost:8000");

type ApiErrorPayload = {
  detail?: string;
};

/** Shared fetch wrapper — forwards ALB session cookie and connection header. */
async function apiFetch(
  path: string,
  init: RequestInit = {},
  connectionId?: string,
): Promise<Response> {
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string> | undefined),
  };
  if (connectionId) {
    headers["X-Connection-Id"] = connectionId;
  }
  // Don't set Content-Type for FormData (browser sets multipart boundary)
  if (!(init.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
    cache: "no-store",
  });
}

async function handleError(response: Response): Promise<never> {
  let errorMessage = `Request failed with status ${response.status}`;
  try {
    const payload = (await response.json()) as ApiErrorPayload;
    if (payload.detail) errorMessage = payload.detail;
  } catch {
    // keep HTTP fallback
  }
  throw new Error(errorMessage);
}

// ── Auth ─────────────────────────────────────────────────────────────

export async function getMe(): Promise<User> {
  const res = await apiFetch("/auth/me");
  if (!res.ok) await handleError(res);
  return (await res.json()) as User;
}

// ── Chat ─────────────────────────────────────────────────────────────

export async function sendChatQuestion(
  question: string,
  debug = false,
  connectionId?: string,
): Promise<ChatResponse> {
  const res = await apiFetch(
    "/chat",
    {
      method: "POST",
      body: JSON.stringify({ question, debug }),
    },
    connectionId,
  );

  if (!res.ok) {
    if (res.status === 400) throw new Error("Invalid/ Unsafe Query");
    await handleError(res);
  }
  return (await res.json()) as ChatResponse;
}

// ── Connections ──────────────────────────────────────────────────────

export async function listConnections(): Promise<ConnectionProfile[]> {
  const res = await apiFetch("/connections");
  if (!res.ok) await handleError(res);
  return (await res.json()) as ConnectionProfile[];
}

export async function testConnection(
  request: ConnectionTestRequest,
): Promise<ConnectionTestResponse> {
  const res = await apiFetch("/connections/test", {
    method: "POST",
    body: JSON.stringify(request),
  });
  if (!res.ok) await handleError(res);
  return (await res.json()) as ConnectionTestResponse;
}

export async function createConnection(
  request: ConnectionCreateRequest,
): Promise<ConnectionCreateResponse> {
  const res = await apiFetch("/connections", {
    method: "POST",
    body: JSON.stringify(request),
  });
  if (!res.ok) await handleError(res);
  return (await res.json()) as ConnectionCreateResponse;
}

export async function uploadFile(
  file: File,
  displayName: string,
): Promise<ConnectionCreateResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("display_name", displayName);

  const res = await apiFetch("/connections/upload", {
    method: "POST",
    body: formData,
  });
  if (!res.ok) await handleError(res);
  return (await res.json()) as ConnectionCreateResponse;
}

export async function generateSchema(
  connectionId: string,
): Promise<GenerateResponse> {
  const res = await apiFetch(`/connections/${connectionId}/generate`, {
    method: "POST",
  });
  if (!res.ok) await handleError(res);
  return (await res.json()) as GenerateResponse;
}

export async function getJobStatus(
  connectionId: string,
  jobId: string,
): Promise<JobStatusResponse> {
  const res = await apiFetch(
    `/connections/${connectionId}/jobs/${jobId}`,
  );
  if (!res.ok) await handleError(res);
  return (await res.json()) as JobStatusResponse;
}

export async function getSchema(connectionId?: string): Promise<SchemaResponse> {
  const res = await apiFetch("/schema", {}, connectionId);
  if (!res.ok) await handleError(res);
  return (await res.json()) as SchemaResponse;
}

export async function publishSchema(
  connectionId: string,
  versionId: string,
): Promise<void> {
  const res = await apiFetch(`/connections/${connectionId}/publish`, {
    method: "POST",
    body: JSON.stringify({ version_id: versionId }),
  });
  if (!res.ok) await handleError(res);
}

export async function deleteConnection(connectionId: string): Promise<void> {
  const res = await apiFetch(`/connections/${connectionId}`, {
    method: "DELETE",
  });
  if (!res.ok) await handleError(res);
}
