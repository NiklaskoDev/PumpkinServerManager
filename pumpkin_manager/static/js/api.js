// HTTP client: JSON in/out, CSRF header on writes, readable errors, job helpers.

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

let csrfToken = null;
let onUnauthorized = () => {};
let onPasswordChangeRequired = () => {};

export function setCsrf(token) { csrfToken = token; }
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }
// An account that signed in with a temporary password may do nothing but choose its own; the server says so with this code.
export function setPasswordChangeHandler(fn) { onPasswordChangeRequired = fn; }

function describe(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).filter((p) => p !== "body").join(".")}: ${d.msg}`).join("; ");
  }
  return fallback;
}

export async function api(method, path, body, options = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const init = { method, headers, credentials: "same-origin" };
  if (body !== undefined && body !== null) {
    if (body instanceof Blob || body instanceof ArrayBuffer) {
      init.body = body;
    } else {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Cannot reach the manager. Is the container still running?");
  }
  let data = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) data = await response.json().catch(() => null);
  if (response.status === 401 && !options.allowUnauthorized) {
    onUnauthorized();
    throw new ApiError(401, describe(data, "Please log in."), data);
  }
  if (response.status === 403 && data && data.code === "password_change_required") {
    onPasswordChangeRequired();
    throw new ApiError(403, describe(data, "Choose a new password first."), data);
  }
  if (!response.ok) throw new ApiError(response.status, describe(data, response.statusText || `HTTP ${response.status}`), data);
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body) => api("POST", path, body ?? {});
export const put = (path, body) => api("PUT", path, body ?? {});
export const patch = (path, body) => api("PATCH", path, body ?? {});
export const del = (path) => api("DELETE", path);

export const qs = (params) => {
  const usable = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return usable.length ? "?" + new URLSearchParams(usable).toString() : "";
};

// Upload with progress (fetch cannot report upload progress, XHR can).
export function upload(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", path);
    if (csrfToken) xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else if (xhr.status === 401) { onUnauthorized(); reject(new ApiError(401, "Please log in.")); }
      else reject(new ApiError(xhr.status, describe(data, `Upload failed (HTTP ${xhr.status})`), data));
    };
    xhr.onerror = () => reject(new ApiError(0, "The upload was interrupted."));
    xhr.send(file);
  });
}
