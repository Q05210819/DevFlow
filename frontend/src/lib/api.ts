let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, public retryAt: number | null = null) {
    super(code);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const code = body && typeof body === "object" && "error" in body &&
      body.error && typeof body.error === "object" && "code" in body.error &&
      typeof body.error.code === "string" ? body.error.code : "request_failed";
    const retry = response.headers.get("Retry-After");
    const deadline = retry === null ? NaN : /^\d+$/.test(retry.trim()) ? Date.now() + Number(retry) * 1000 : Date.parse(retry);
    throw new ApiError(response.status, code, Number.isFinite(deadline) ? deadline : null);
  }
  return response.status === 204 ? undefined as T : await response.json() as T;
}
