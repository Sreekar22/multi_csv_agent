import type {
  ChatRequest,
  ChatResponse,
  SessionCreateResponse,
  SessionQualityResponse,
  SessionSchemaResponse,
  UploadFilesResponse,
} from "@/types/api";

const ENV_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "");
const API_BASE_CANDIDATES = ENV_API_BASE_URL
  ? [ENV_API_BASE_URL]
  : ["http://127.0.0.1:8017", "http://localhost:8017", "http://127.0.0.1:8000", "http://localhost:8000"];

let resolvedApiBaseUrl: string | null = ENV_API_BASE_URL || null;

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    const message = payload.detail || `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return (await response.json()) as T;
}

async function fetchWithApiFallback(path: string, init?: RequestInit): Promise<Response> {
  const candidates = resolvedApiBaseUrl ? [resolvedApiBaseUrl] : API_BASE_CANDIDATES;
  let lastResponse: Response | null = null;
  let lastError: unknown = null;

  for (const baseUrl of candidates) {
    try {
      const response = await fetch(`${baseUrl}${path}`, init);
      if (response.ok) {
        resolvedApiBaseUrl = baseUrl;
        return response;
      }

      lastResponse = response;
      if (response.status < 500) {
        return response;
      }
    } catch (error) {
      lastError = error;
    }
  }

  if (lastResponse) {
    return lastResponse;
  }

  const message =
    lastError instanceof Error
      ? lastError.message
      : "Could not reach backend API. Check NEXT_PUBLIC_API_BASE_URL or run backend.";
  throw new Error(message);
}

export async function createSession(): Promise<SessionCreateResponse> {
  const response = await fetchWithApiFallback(`/api/v1/sessions`, {
    method: "POST",
  });

  return parseResponse<SessionCreateResponse>(response);
}

export async function uploadSessionFiles(
  sessionId: string,
  files: File[],
  options?: { cleanData?: boolean },
): Promise<UploadFilesResponse> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  formData.append("clean_data", options?.cleanData ? "true" : "false");

  const response = await fetchWithApiFallback(`/api/v1/sessions/${sessionId}/files`, {
    method: "POST",
    body: formData,
  });

  return parseResponse<UploadFilesResponse>(response);
}

export async function getSessionSchema(
  sessionId: string,
): Promise<SessionSchemaResponse> {
  const response = await fetchWithApiFallback(`/api/v1/sessions/${sessionId}/schema`);
  return parseResponse<SessionSchemaResponse>(response);
}

export async function getSessionQualityReport(
  sessionId: string,
): Promise<SessionQualityResponse> {
  const response = await fetchWithApiFallback(
    `/api/v1/sessions/${sessionId}/quality-report`,
  );
  return parseResponse<SessionQualityResponse>(response);
}

export async function sendChatMessage(
  sessionId: string,
  payload: ChatRequest,
): Promise<ChatResponse> {
  const response = await fetchWithApiFallback(`/api/v1/sessions/${sessionId}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseResponse<ChatResponse>(response);
}
