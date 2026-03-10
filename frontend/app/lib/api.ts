import { ChatResponse } from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

type ApiErrorPayload = {
  detail?: string;
};

export async function sendChatQuestion(question: string, debug = false): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ question, debug }),
    cache: "no-store",
  });

  if (!response.ok) {
    let errorMessage = `Request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as ApiErrorPayload;
      if (payload.detail) {
        errorMessage = payload.detail;
      }
    } catch {
      // Keep the HTTP fallback if the error body is not JSON.
    }
    throw new Error(errorMessage);
  }

  return (await response.json()) as ChatResponse;
}
