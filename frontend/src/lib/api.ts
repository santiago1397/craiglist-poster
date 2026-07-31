// Thin fetch wrapper. Always sends cookies (credentials: include) and JSON.
// Base URL comes from VITE_API_BASE_URL at build time; falls back to /api for
// the local dev proxy defined in vite.config.ts.

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/**
 * Called once when the server rejects a request as unauthenticated.
 *
 * The session cookie lasts 30 days. When it expired with the tab open, every
 * query started failing and you were left on a dead page full of error banners
 * with no indication that the fix was simply logging in again. AuthProvider
 * registers a handler that clears the user and sends you to /login.
 *
 * Suppressed while a login attempt is in flight — a wrong password is a 401
 * that the form should report itself, not a session expiry.
 */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;
let suppressed = false;

export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn;
}

export async function withoutSessionRedirect<T>(fn: () => Promise<T>): Promise<T> {
  suppressed = true;
  try {
    return await fn();
  } finally {
    suppressed = false;
  }
}

function buildUrl(path: string, searchParams?: Record<string, string | number | undefined | null>): string {
  // Concatenate BASE + path directly so BASE's own path (e.g. "/api") is
  // preserved. new URL(absolutePath, base) would discard base's path segment.
  const clean = `${BASE.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
  if (!searchParams) return clean;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(searchParams)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${clean}?${s}` : clean;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  searchParams?: Record<string, string | number | undefined | null>,
): Promise<T> {
  const res = await fetch(buildUrl(path, searchParams), {
    method,
    credentials: "include",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && !suppressed) onUnauthorized?.();
    const msg = (parsed && typeof parsed === "object" && "detail" in parsed
      ? String((parsed as any).detail)
      : `HTTP ${res.status}`);
    throw new ApiError(res.status, parsed, msg);
  }
  return parsed as T;
}

export const api = {
  get: <T,>(path: string, params?: Record<string, string | number | undefined | null>) =>
    request<T>("GET", path, undefined, params),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T,>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T,>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  // 204 responses have no body; request() resolves them as null.
  del: <T,>(path: string) => request<T>("DELETE", path),
};
