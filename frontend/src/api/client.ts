export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
  get code(): string | undefined {
    return typeof this.detail === "object" && this.detail !== null && "code" in this.detail
      ? String((this.detail as { code: unknown }).code)
      : undefined;
  }
}

function messageFrom(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    if ("message" in detail) return String((detail as { message: unknown }).message);
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  }
  return status === 0 ? "Can't reach the server" : `Something went wrong (${status})`;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Requested-With", "aivo");
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { ...init, headers, body, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, null, "Can't reach the server");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    const detail = data?.detail ?? data;
    throw new ApiError(res.status, detail, messageFrom(detail, res.status));
  }
  return data as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, json?: unknown, headers?: HeadersInit) =>
  api<T>(path, { method: "POST", json: json ?? {}, headers });
export const put = <T>(path: string, json: unknown) => api<T>(path, { method: "PUT", json });
export const patch = <T>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

export async function upload<T>(path: string, file: File): Promise<T> {
  const fd = new FormData();
  fd.append("file", file);
  return api<T>(path, { method: "POST", body: fd });
}
