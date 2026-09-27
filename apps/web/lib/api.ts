const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const ACCESS_TOKEN_KEY = "yonixalpha_access_token";
const REFRESH_TOKEN_KEY = "yonixalpha_refresh_token";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export function storeTokens(tokens: TokenPair): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
  localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function login(username: string, password: string): Promise<TokenPair> {
  const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await res.json();
  if (!res.ok) {
    throw new ApiError(res.status, body.detail ?? "Login failed");
  }
  return body as TokenPair;
}

export async function logout(): Promise<void> {
  const refreshToken = getRefreshToken();
  if (refreshToken) {
    await fetch(`${API_BASE_URL}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    }).catch(() => undefined);
  }
  clearTokens();
}

/** GETs `path` with the given query params, attached via apiFetch (bearer
 * token + one 401 refresh-and-retry). Throws ApiError on a non-2xx
 * response rather than returning a Response the caller has to check —
 * every dashboard page wants "the data, or an error to show," never a
 * raw Response to unwrap itself.
 */
export async function apiGet<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const query = params
    ? "?" +
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== "")
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join("&")
    : "";
  const res = await apiFetch(`${path}${query}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

/** Settings writes return the new runtime configuration revision in
 * X-Config-Revision (apps/api config_revision middleware). Announced so the
 * dashboard can show whether the running services applied it. */
export const CONFIG_SAVED_EVENT = "yx:config-saved";
export let lastConfigRevision: number | null = null;

function announceRevision(res: Response): void {
  const rev = res.headers.get("x-config-revision");
  if (!rev || typeof window === "undefined") return;
  lastConfigRevision = Number(rev);
  window.dispatchEvent(new CustomEvent(CONFIG_SAVED_EVENT, { detail: { revision: Number(rev) } }));
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, describeDetail(errBody.detail) ?? `Request failed (${res.status})`);
  }
  announceRevision(res);
  return res.json() as Promise<T>;
}

async function apiSend<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, describeDetail(errBody.detail) ?? `Request failed (${res.status})`);
  }
  announceRevision(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** FastAPI error details come as a string, {errors: [...]}, or a list of
 * pydantic validation errors — flattened here into one readable line. */
export function describeDetail(detail: unknown): string | undefined {
  if (detail === undefined || detail === null) return undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : String(d))).join("; ");
  if (typeof detail === "object" && detail && "errors" in detail) return (detail as { errors: string[] }).errors.join("; ");
  return JSON.stringify(detail);
}

export const apiPut = <T>(path: string, body?: unknown) => apiSend<T>("PUT", path, body);
export const apiPatch = <T>(path: string, body?: unknown) => apiSend<T>("PATCH", path, body);
export const apiDelete = (path: string) => apiSend<void>("DELETE", path);

/** Authenticated fetch — attaches the bearer token, and on a 401 attempts
 * exactly one refresh-and-retry before giving up (no infinite retry loop).
 */
export async function apiFetch(path: string, init: RequestInit = {}, isRetry = false): Promise<Response> {
  const token = getAccessToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });

  if (res.status === 401 && !isRetry) {
    const refreshed = await tryRefresh();
    if (refreshed) return apiFetch(path, init, true);
  }
  return res;
}

async function tryRefresh(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${API_BASE_URL}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      clearTokens();
      return false;
    }
    const tokens = (await res.json()) as TokenPair;
    storeTokens(tokens);
    return true;
  } catch {
    return false;
  }
}
