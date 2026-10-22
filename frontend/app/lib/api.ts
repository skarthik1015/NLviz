import {
  AgentStep,
  ChatResponse,
  ConnectionCreateRequest,
  ConnectionCreateResponse,
  ConnectionProfile,
  ConnectionTestRequest,
  ConnectionTestResponse,
  GenerateResponse,
  JobStatusResponse,
  S3ImportRequest,
  AttachFileResponse,
  SchemaResponse,
  User,
} from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  (typeof window !== "undefined"
    ? `${window.location.origin}/api`
    : "http://localhost:8000");

/** FastAPI sends `detail` as a string ("CODE: explanation") or, for request validation, a list. */
type ApiErrorPayload = {
  detail?: string | Array<{ loc?: unknown[]; msg?: string }>;
};

/** An API failure: `code` is the backend's code ("SCHEMA_MISSING") when there is one, `message`
 * the text for the user. Compare codes, never messages. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string | null,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function errorCode(detail: string): string | null {
  return /^([A-Z][A-Z_]{4,})(?::|$)/.exec(detail.trim())?.[1] ?? null;
}

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

/** Plain-language text for the API's error codes ("CONNECTION_ARCHIVED", or "CODE: detail"). */
const ERROR_MESSAGES: Record<string, string> = {
  CONNECTION_ARCHIVED: "This connection was deleted. Pick another connection or connect again.",
  CONNECTION_NOT_FOUND: "This connection no longer exists. Pick another connection.",
  CONNECTION_NOT_READY: "This connection has no published data model yet. Generate and publish one first.",
  SCHEMA_NOT_PUBLISHED: "This connection has no published data model yet. Generate and publish one first.",
  SCHEMA_MISSING: "This connection's data model is missing. Generate and publish it again.",
  CONNECTION_UNAVAILABLE: "The data source can't be reached right now. Try again in a moment.",
  DATABASE_UNAVAILABLE: "Your database isn't responding right now. Check that it's running, then try again.",
  DATABASE_NOT_FOUND: "That database doesn't exist on the server. Check the database name.",
  CONNECTOR_BUILD_FAILED: "The data source can't be reached right now. Try again in a moment.",
  INVALID_OR_UNSAFE_QUERY: "This question couldn't be turned into a safe query. Try rephrasing it.",
  QUESTION_NOT_UNDERSTOOD:
    "I couldn't interpret that; try rephrasing.",
  AUTH_FAILED: "Wrong username or password.",
  NETWORK_UNREACHABLE: "The database host can't be reached. Check the host name, port and firewall.",
  TIMEOUT: "The request took too long. Try again, or narrow the question.",
  INVALID_CONNECTION_ID: "Pick a connection first.",
};

export function friendlyError(detail: string): string {
  const match = /^([A-Z][A-Z_]{4,})(?::\s*(.*))?$/s.exec(detail.trim());
  if (!match) return detail;
  const [, code, rest] = match;
  const known = ERROR_MESSAGES[code];
  if (code === "QUESTION_NOT_UNDERSTOOD") return known;
  if (known) return rest ? `${known} (${rest})` : known;
  // An unmapped code with an explanation ("NO_TABLES: the 'public' schema has no tables"): show the explanation.
  return rest ? rest.charAt(0).toUpperCase() + rest.slice(1) : "Something went wrong. Please try again.";
}

async function handleError(response: Response): Promise<never> {
  let message = `Request failed with status ${response.status}`;
  let code: string | null = null;
  try {
    const payload = (await response.json()) as ApiErrorPayload;
    if (typeof payload.detail === "string") {
      code = errorCode(payload.detail);
      message = friendlyError(payload.detail);
    } else if (Array.isArray(payload.detail) && payload.detail.length > 0) {
      code = "INVALID_REQUEST";
      message = payload.detail.map((item) => item.msg ?? "invalid value").join("; ");
    }
  } catch {
    // keep HTTP fallback
  }
  throw new ApiError(message, code, response.status);
}

// ── Auth ─────────────────────────────────────────────────────────────

export async function getMe(): Promise<User> {
  const res = await apiFetch("/auth/me");
  if (!res.ok) await handleError(res);
  return (await res.json()) as User;
}

// ── Chat ─────────────────────────────────────────────────────────────

/** Reads the server-sent events of a /chat stream: `step` events go to `onStep`, the final
 * `result` is returned, an `error` event is thrown. */
async function readChatStream(res: Response, onStep: (step: AgentStep) => void): Promise<ChatResponse> {
  if (!res.ok || !res.body) {
    await handleError(res);
  }
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let result: ChatResponse | null = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      const payload = JSON.parse(data);
      if (event === "step") onStep(payload as AgentStep);
      else if (event === "result") result = payload as ChatResponse;
      else if (event === "error") {
        const detail = String(payload.detail ?? "Request failed");
        throw new ApiError(friendlyError(detail), errorCode(detail), Number(payload.status ?? 500));
      }
    }
  }
  if (!result) throw new Error("The answer stream ended before a result arrived.");
  return result;
}

/** Ask a question in a conversation, streaming each agent step. */
export async function streamChatQuestion(
  question: string,
  options: { conversationId?: string; debug?: boolean; connectionId?: string; signal?: AbortSignal },
  onStep: (step: AgentStep) => void,
): Promise<ChatResponse> {
  const res = await apiFetch(
    "/chat/stream",
    {
      method: "POST",
      body: JSON.stringify({ question, debug: options.debug ?? false, conversation_id: options.conversationId }),
      signal: options.signal,
    },
    options.connectionId,
  );
  return readChatStream(res, onStep);
}

/** Answer the clarification a conversation is paused on, streaming the rest of the turn. */
export async function streamClarificationAnswer(
  conversationId: string,
  answer: string,
  options: { debug?: boolean; connectionId?: string; signal?: AbortSignal; remember?: boolean },
  onStep: (step: AgentStep) => void,
): Promise<ChatResponse> {
  const res = await apiFetch(
    "/chat/resume/stream",
    {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId, answer, debug: options.debug ?? false,
                             remember: options.remember ?? false }),
      signal: options.signal,
    },
    options.connectionId,
  );
  return readChatStream(res, onStep);
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

export async function attachFile(connectionId: string, file: File): Promise<AttachFileResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await apiFetch(`/connections/${connectionId}/files`, { method: "POST", body: formData });
  if (!res.ok) await handleError(res);
  return (await res.json()) as AttachFileResponse;
}

export async function importFromS3(request: S3ImportRequest): Promise<ConnectionCreateResponse> {
  const res = await apiFetch("/connections/s3-import", {
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
