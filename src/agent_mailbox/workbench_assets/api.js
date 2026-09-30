export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const storageKey = "agent-mailbox.workbench.token";
let token = "";
const fragment = new URLSearchParams(window.location.hash.slice(1));
if (fragment.has("token")) {
  token = fragment.get("token") || "";
  // Remove the credential from the URL before any request or interaction.
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  try { window.sessionStorage.setItem(storageKey, token); } catch { /* this tab still works */ }
} else {
  try { token = window.sessionStorage.getItem(storageKey) || ""; } catch { /* no persisted authorization */ }
}

async function request(path, method = "GET", body) {
  if (!token) throw new ApiError("authorization_required", 401);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15000);
  try {
    const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`/api/workbench${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", signal: controller.signal, cache: "no-store",
    });
    const text = await response.text();
    let result;
    try { result = text ? JSON.parse(text) : {}; } catch { throw new ApiError("invalid_response", response.status); }
    if (!response.ok) {
      const error = new ApiError(result?.error?.message || "request_failed", response.status);
      error.code = result?.error?.code || "request_failed";
      throw error;
    }
    return result;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(error.name === "AbortError" ? "request_timeout" : "connection_failed");
  } finally { window.clearTimeout(timeout); }
}

const id = (value) => encodeURIComponent(value);
function subscribeChanges(onChange, onError, onConnected = () => {}) {
  let stopped = false;
  let controller = null;
  let reconnectTimer;
  let endReconnect;
  let lastRevision = null;
  async function listen() {
    if (!token) { onError(new ApiError("authorization_required", 401)); return; }
    while (!stopped) {
      controller = new AbortController();
      let reader;
      try {
        const response = await fetch("/api/workbench/changes", {
          headers: { Authorization: `Bearer ${token}`, Accept: "application/x-ndjson" },
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) {
          let value;
          try { value = await response.json(); } catch { /* use request status */ }
          throw new ApiError(value?.error?.message || "request_failed", response.status);
        }
        if (!response.body) throw new ApiError("invalid_response");
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = "";
        let connected = false;
        while (!stopped) {
          const chunk = await reader.read();
          if (chunk.done) throw new ApiError("connection_failed");
          pending += decoder.decode(chunk.value, { stream: true });
          if (pending.length > 1048576) throw new ApiError("invalid_response");
          let boundary;
          while ((boundary = pending.indexOf("\n")) !== -1) {
            const line = pending.slice(0, boundary).trim();
            pending = pending.slice(boundary + 1);
            if (!line) continue;
            let change;
            try { change = JSON.parse(line); } catch { throw new ApiError("invalid_response"); }
            if (!Number.isSafeInteger(change.revision)) throw new ApiError("invalid_response");
            if (!connected) { connected = true; onConnected(); }
            if (lastRevision === null) lastRevision = change.revision;
            else if (lastRevision !== change.revision) { lastRevision = change.revision; onChange(change); }
          }
        }
      } catch (error) {
        if (!stopped) onError(error instanceof ApiError ? error : new ApiError("connection_failed"));
      } finally {
        if (reader) { try { await reader.cancel(); } catch { /* connection closed */ } }
      }
      if (!stopped) await new Promise((resolve) => { endReconnect = resolve; reconnectTimer = window.setTimeout(resolve, 5000); });
    }
  }
  listen();
  return () => { stopped = true; controller?.abort(); window.clearTimeout(reconnectTimer); endReconnect?.(); };
}
export const api = Object.freeze({
  tokenAvailable: Boolean(token),
  subscribeChanges,
  snapshot: () => request("/bootstrap"),
  createProject: (body) => request("/projects", "POST", body),
  pickProject: () => request("/pick-project", "POST", {}),
  discoverEmployees: () => request("/discover"),
  models: (kind) => request(`/models?kind=${id(kind)}`),
  addEmployee: (body) => request("/employees", "POST", body),
  createTask: (body) => request("/tasks", "POST", body),
  taskDetail: (taskId) => request(`/tasks/${id(taskId)}`),
  cancelTask: (taskId) => request(`/tasks/${id(taskId)}/cancel`, "POST", {}),
  reviewTask: (taskId, body) => request(`/tasks/${id(taskId)}/review`, "POST", body),
  decidePermission: (taskId, requestId, body) => request(`/tasks/${id(taskId)}/permissions/${id(requestId)}`, "POST", body),
  addResource: (body) => request("/resources", "POST", body),
  addMemory: (body) => request("/memories", "POST", body),
  deleteMemory: (memoryId) => request(`/memories/${id(memoryId)}`, "DELETE"),
  searchMemory: (projectId, query) => request(`/memory/search?project_id=${id(projectId)}&q=${id(query)}`),
  installRuntime: () => request("/runtime/install", "POST", {}),
  startFleet: (body) => request("/fleet/start", "POST", body),
  stopFleet: () => request("/fleet/stop", "POST", {}),
  inviteDevice: (projectIds) => request("/fleet/invite", "POST", { project_ids: projectIds }),
  joinFleet: (invite) => request("/fleet/join", "POST", { invite }),
  leaveFleet: () => request("/fleet/leave", "POST", {}),
  mapRemoteProject: (body) => request("/fleet/map", "POST", body),
  addRemoteEmployee: (body) => request("/fleet/employee", "POST", body),
  revokeDevice: (deviceId) => request("/fleet/revoke", "POST", { device_id: deviceId }),
});
