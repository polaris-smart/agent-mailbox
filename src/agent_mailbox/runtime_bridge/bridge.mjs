// Python owns admission and project identity. This process owns ACP execution only.
import fs from "node:fs/promises";
import path from "node:path";
import readline from "node:readline";
import { randomUUID, createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

const assetDir = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const options = {};
for (let i = 0; i < argv.length; i += 2) {
  if (!argv[i]?.startsWith("--") || argv[i + 1] === undefined) {
    process.stderr.write("Bridge requires named arguments with values.\n");
    process.exit(2);
  }
  options[argv[i].slice(2)] = argv[i + 1];
}
const runs = new Map();
const usedRunIds = new Set();
const sessions = new Map();
let shuttingDown = false;
let startupError;
let runtimeApi;
let projectRoot;
let stateDir;
let runtimeDir;
let testCommand;

class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
function fail(code, message) { throw new BridgeError(code, message); }
function string(value, name, max = 1024) {
  if (typeof value !== "string" || !value.trim() || value.length > max || value.includes("\0")) {
    fail("INVALID_REQUEST", `${name} must be a nonempty string of at most ${max} characters`);
  }
  return value;
}
function milliseconds(value, fallback, name) {
  const result = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(result) || result < 1 || result > 86400000) {
    fail("INVALID_REQUEST", `${name} must be an integer between 1 and 86400000`);
  }
  return result;
}
// Never forward raw child stderr, credentials, or an environment snapshot.
function safeMessage(value) {
  let text = String(value ?? "Agent execution failed");
  for (const [key, secret] of Object.entries(process.env)) {
    if (/TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION/i.test(key) && secret?.length >= 8) {
      text = text.split(secret).join("[redacted]");
    }
  }
  return text.replace(/\bsk-[A-Za-z0-9_-]{8,}/g, "[redacted]")
    .replace(/(Bearer\s+)\S+/gi, "$1[redacted]").slice(0, 4000);
}
// Dependencies must not contaminate stdout or dump SDK/child diagnostic payloads.
for (const method of ["log", "error", "warn", "debug", "info"]) {
  console[method] = () => { process.stderr.write("ACP runtime diagnostic; inspect the structured run result.\n"); };
}
function errorInfo(error, resume = false) {
  const message = safeMessage(error?.message);
  const chain = `${error?.code ?? ""} ${error?.detailCode ?? ""} ${message} ${safeMessage(error?.cause?.message)}`;
  let code = error?.code;
  if (/AUTH_REQUIRED|authentication required|auth required|login required|not logged in/i.test(chain)) code = "AUTH_REQUIRED";
  else if (/TIMEOUT|timed out|timeout/i.test(chain)) code = "TIMEOUT";
  else if (resume && !["INVALID_REQUEST", "SESSION_CONTEXT_MISMATCH"].includes(code)) code = "SESSION_RESUME_FAILED";
  else if (/ENOENT|ACP_BACKEND_MISSING|executable.*not found/i.test(chain)) code = "AGENT_UNAVAILABLE";
  else if (!code || code.startsWith("ACP_") || code === "RUNTIME") code = "AGENT_FAILED";
  return { code, message, retryable: error?.retryable === true };
}
function emit(run, type, payload = {}) {
  if (run?.terminal && type !== "result") return;
  process.stdout.write(JSON.stringify({ protocol: 1, run_id: run?.id ?? null,
    session_id: run?.sessionId ?? null, type, ...payload }) + "\n");
}
function terminal(run, result) {
  if (run.terminal) return;
  run.terminal = true;
  emit(run, "result", { output_text: run.output.join(""), ...result });
  runs.delete(run.id);
  for (const pending of run.permissions.values()) pending.finish({ outcome: "cancel" });
}
function controlError(message, input = {}) {
  emit({ id: input.run_id, sessionId: input.session_id }, "control_error", {
    error: { code: "INVALID_REQUEST", message: safeMessage(message) },
  });
}
async function writeJson(filename, value) {
  await fs.mkdir(path.dirname(filename), { recursive: true, mode: 0o700 });
  const temporary = `${filename}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(value), { mode: 0o600 });
  await fs.rename(temporary, filename);
}
async function readJson(filename) {
  try { return JSON.parse(await fs.readFile(filename, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return undefined; throw error; }
}
async function boundedCwd(value) {
  const requested = value ?? projectRoot;
  if (typeof requested !== "string" || !path.isAbsolute(requested)) fail("INVALID_REQUEST", "cwd must be absolute");
  let actual;
  try { actual = await fs.realpath(requested); }
  catch { fail("INVALID_REQUEST", "cwd does not exist"); }
  const relative = path.relative(projectRoot, actual);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    fail("PROJECT_BOUNDARY", "cwd is outside the configured project root");
  }
  if (!(await fs.stat(actual)).isDirectory()) fail("INVALID_REQUEST", "cwd must be a directory");
  return actual;
}
function validateMcp(servers) {
  if (!Array.isArray(servers) || servers.length > 32) fail("INVALID_REQUEST", "mcp_servers must be an array of at most 32 entries");
  const names = new Set();
  return servers.map(server => {
    if (!server || typeof server !== "object") fail("INVALID_REQUEST", "Invalid MCP server");
    const name = string(server.name, "MCP name", 128);
    if (names.has(name)) fail("INVALID_REQUEST", "Duplicate MCP server name");
    names.add(name);
    if (server.type === "http") {
      let url;
      try { url = new URL(server.url); } catch { fail("INVALID_REQUEST", "Invalid MCP URL"); }
      if (!["http:", "https:"].includes(url.protocol)) fail("INVALID_REQUEST", "MCP URL must be HTTP(S)");
      const headers = server.headers ?? [];
      if (!Array.isArray(headers) || headers.some(h => typeof h?.name !== "string" || typeof h?.value !== "string")) {
        fail("INVALID_REQUEST", "Invalid MCP headers");
      }
      return { type: "http", name, url: url.href, headers };
    }
    if (server.type && server.type !== "stdio") fail("INVALID_REQUEST", "Only stdio and HTTP MCP are supported");
    if (!path.isAbsolute(string(server.command, "MCP command"))) fail("INVALID_REQUEST", "MCP command must be absolute");
    if (!Array.isArray(server.args) || server.args.some(a => typeof a !== "string" || a.includes("\0"))) fail("INVALID_REQUEST", "Invalid MCP args");
    const env = server.env ?? [];
    if (!Array.isArray(env) || env.some(e => typeof e?.name !== "string" || typeof e?.value !== "string")) fail("INVALID_REQUEST", "Invalid MCP env");
    return { name, command: server.command, args: server.args, env };
  });
}
async function adapterCommand(agent) {
  if (testCommand) return testCommand;
  const packageName = agent === "codex" ? "codex-acp" : "claude-agent-acp";
  const packageDir = path.join(runtimeDir, "node_modules", "@agentclientprotocol", packageName);
  let metadata;
  try { metadata = await readJson(path.join(packageDir, "package.json")); }
  catch { fail("AGENT_UNAVAILABLE", `${agent} ACP adapter is unavailable; install the managed runtime`); }
  const executable = metadata?.bin?.[packageName];
  if (!executable) fail("AGENT_UNAVAILABLE", `${agent} ACP adapter is unavailable; install the managed runtime`);
  const launcher = path.join(packageDir, executable);
  try { await fs.access(launcher); } catch { fail("AGENT_UNAVAILABLE", `${agent} ACP adapter launcher is missing`); }
  return [process.execPath, launcher];
}
async function deadline(promise, duration, onTimeout) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => { onTimeout?.(); reject(new BridgeError("TIMEOUT", "Agent startup timed out")); }, duration);
    })]);
  } finally { clearTimeout(timer); }
}
async function sessionFor(run, input) {
  const cwd = await boundedCwd(input.cwd);
  if (shuttingDown || run.controller.signal.aborted) fail("CANCELLED", "Run cancelled before dispatch");
  const sandbox = input.sandbox ?? "read-only";
  if (!["read-only", "workspace-write"].includes(sandbox)) fail("INVALID_REQUEST", "Unsupported sandbox mode");
  if (input.agent === "claude" && sandbox === "workspace-write" && !testCommand) {
    fail("SANDBOX_UNSUPPORTED", "Claude plan mode is available; a workspace OS sandbox is not provided by this adapter");
  }
  const mcp = validateMcp(input.mcp_servers ?? []);
  const digest = createHash("sha256").update(JSON.stringify(mcp)).digest("hex");
  run.nativeMode = input.agent === "claude" && !testCommand ? "plan" : sandbox;
  const context = { agent: input.agent, cwd };
  let entry = sessions.get(run.sessionId);
  if (entry) {
    if (entry.digest !== digest || JSON.stringify(entry.context) !== JSON.stringify(context)) {
      fail("SESSION_CONTEXT_MISMATCH", "Live managed session context changed; reconnect it explicitly");
    }
    return entry;
  }
  entry = { context, digest };
  sessions.set(run.sessionId, entry);
  entry.ready = (async () => {
    const bindingPath = path.join(stateDir, "bindings", `${encodeURIComponent(run.sessionId)}.json`);
    let binding;
    try { binding = await readJson(bindingPath); }
    catch { fail("SESSION_RESUME_FAILED", "Managed session identity binding is invalid"); }
    if (binding && JSON.stringify(binding.context) !== JSON.stringify(context)) {
      fail("SESSION_CONTEXT_MISMATCH", "Managed session agent or project directory changed");
    }
    if (binding?.backend_session_id && input.resume_session_id && binding.backend_session_id !== input.resume_session_id) {
      fail("SESSION_CONTEXT_MISMATCH", "Resume ID does not match this managed session");
    }
    const command = await adapterCommand(input.agent);
    const store = runtimeApi.createFileSessionStore({ stateDir });
    entry.runtime = runtimeApi.createAcpRuntime({
      cwd, sessionStore: {
        async load(id) {
          const record = await store.load(id);
          const filename = path.join(stateDir, "sessions", `${encodeURIComponent(id)}.json`);
          if (!record) {
            try { await fs.access(filename); fail("SESSION_RESUME_FAILED", "Managed session state is invalid"); }
            catch (error) { if (error.code !== "ENOENT") throw error; }
          }
          return record;
        },
        async save(record) {
          record.eventLog.active_path = path.join(stateDir, "sessions", `${encodeURIComponent(record.acpxRecordId)}.stream.ndjson`);
          await store.save(record);
        },
      },
      agentRegistry: { resolve: () => command, list: () => [input.agent] },
      mcpServers: () => mcp,
      permissionMode: "deny-all", nonInteractivePermissions: "deny", fs: false, terminal: false,
      agentProcessEnv: { INITIAL_AGENT_MODE: "read-only" },
      timeoutMs: startupTimeout,
    });
    const resumeId = input.resume_session_id ?? binding?.backend_session_id;
    entry.resuming = Boolean(resumeId || binding);
    try {
      const setup = (async () => {
        const handle = await entry.runtime.ensureSession({ sessionKey: run.sessionId, agent: input.agent,
          mode: "persistent", cwd, ...(resumeId ? { resumeSessionId: resumeId } : {}) });
        await writeJson(bindingPath, { context, backend_session_id: handle.backendSessionId });
        return handle;
      })();
      entry.handle = await deadline(setup, startupTimeout, () => { void entry.runtime.shutdown().catch(() => {}); });
      return entry.handle;
    } catch (error) {
      await entry.runtime.shutdown().catch(() => {});
      throw Object.assign(new Error(errorInfo(error, entry.resuming).message), errorInfo(error, entry.resuming));
    }
  })();
  // Initialization continues for another queued run after one waiting run is cancelled.
  entry.ready.catch(() => { if (sessions.get(run.sessionId) === entry) sessions.delete(run.sessionId); });
  return entry;
}
async function waitActive(promise, run) {
  if (run.controller.signal.aborted) fail("CANCELLED", "Run cancelled before dispatch");
  let listener;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      listener = () => reject(new BridgeError("CANCELLED", "Run cancelled before dispatch"));
      run.controller.signal.addEventListener("abort", listener, { once: true });
    })]);
  } finally { run.controller.signal.removeEventListener("abort", listener); }
}
async function acquireSessionTurn(entry, run) {
  const previous = entry.tail ?? Promise.resolve();
  let release;
  const held = new Promise(resolve => { release = resolve; });
  // The native mode control and its turn share one admission slot. A queued
  // permission upgrade must not change a currently running turn's sandbox.
  entry.tail = previous.catch(() => {}).then(() => held);
  try { await waitActive(previous, run); return release; }
  catch (error) { release(); throw error; }
}
function permission(run, request, { signal }) {
  const requestId = randomUUID();
  return new Promise(resolve => {
    let timer;
    const finish = decision => {
      if (!run.permissions.delete(requestId)) return;
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      resolve(decision);
    };
    const abort = () => finish({ outcome: "cancel" });
    run.permissions.set(requestId, { finish });
    signal.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => finish({ outcome: "reject_once" }), run.permissionTimeout);
    if (signal.aborted || run.terminal) abort();
    else emit(run, "permission_required", { request_id: requestId, request: request.raw,
      options: request.raw.options, tool_call: request.raw.toolCall,
      inferred_kind: request.inferredKind ?? null, timeout_ms: run.permissionTimeout });
  });
}
async function execute(run, input) {
  let release;
  try {
    if (startupError) throw startupError;
    if (shuttingDown) fail("INVALID_REQUEST", "Bridge is shutting down");
    if (!["codex", "claude"].includes(input.agent)) fail("INVALID_REQUEST", "Only codex and claude managed adapters are supported");
    if (typeof input.prompt !== "string" || input.prompt.includes("\0") || input.prompt.length > 2000000) fail("INVALID_REQUEST", "prompt must be a string of at most 2000000 characters");
    if (input.resume_session_id !== undefined) string(input.resume_session_id, "resume_session_id");
    const timeout = milliseconds(input.timeout_ms, 600000, "timeout_ms");
    run.permissionTimeout = milliseconds(input.permission_timeout_ms, permissionTimeout, "permission_timeout_ms");
    const entry = await sessionFor(run, input);
    const handle = await waitActive(entry.ready, run);
    if (input.resume_session_id && input.resume_session_id !== handle.backendSessionId) {
      fail("SESSION_CONTEXT_MISMATCH", "Resume ID does not match this managed session");
    }
    release = await acquireSessionTurn(entry, run);
    try {
      await deadline(entry.runtime.setMode({ handle, mode: run.nativeMode, signal: run.controller.signal }),
        startupTimeout, () => { void entry.runtime.shutdown().catch(() => {}); sessions.delete(run.sessionId); });
    } catch (error) {
      if (run.controller.signal.aborted) fail("CANCELLED", "Run cancelled before dispatch");
      if (error.code === "TIMEOUT") throw error;
      fail("SANDBOX_UNSUPPORTED", "Agent adapter does not support the requested native permission mode");
    }
    emit(run, "session", { backend_session_id: handle.backendSessionId,
      acpx_record_id: handle.acpxRecordId, native_mode: run.nativeMode });
    const turn = entry.runtime.startTurn({ handle, text: input.prompt, mode: "prompt", requestId: run.id,
      signal: run.controller.signal, timeoutMs: timeout,
      onPermissionRequest: (request, context) => permission(run, request, context) });
    run.turn = turn;
    const started = turn.promptStarted.then(() => emit(run, "started"));
    started.catch(() => {});
    for await (const event of turn.events) {
      if (event.type === "text_delta" && event.stream !== "thought") run.output.push(event.text);
      emit(run, "event", { event });
    }
    const result = await turn.result;
    await started.catch(() => {});
    if (result.status === "failed") {
      terminal(run, { status: "failed", error: errorInfo(result.error),
        runtime_result: { ...result, error: errorInfo(result.error) } });
    } else terminal(run, { status: result.status, stop_reason: result.stopReason ?? null, runtime_result: result });
  } catch (error) {
    terminal(run, error.code === "CANCELLED"
      ? { status: "cancelled", stop_reason: "cancelled" }
      : { status: "failed", error: errorInfo(error) });
  } finally { release?.(); }
}
async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const run of runs.values()) run.controller.abort();
  await Promise.allSettled([...sessions.values()].map(entry => entry.runtime?.shutdown()));
  await Promise.allSettled([...runs.values()].map(run => run.task));
}
function command(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return controlError("Expected a JSON object");
  if (input.op === "run") {
    let id, sessionId;
    try { id = string(input.run_id, "run_id", 128); sessionId = string(input.session_id, "session_id", 128); }
    catch (error) { return controlError(error.message, input); }
    if (usedRunIds.has(id)) return controlError("run_id has already been admitted", input);
    usedRunIds.add(id);
    const run = { id, sessionId, output: [], permissions: new Map(), controller: new AbortController(), terminal: false };
    runs.set(id, run);
    run.task = execute(run, input);
  } else if (input.op === "cancel") {
    const run = runs.get(input.run_id);
    if (!run) return controlError("No active run with this run_id", input);
    run.controller.abort();
    if (run.turn) void run.turn.cancel({ reason: "Human cancelled run" }).catch(() => {});
  } else if (input.op === "permission") {
    const run = runs.get(input.run_id);
    const pending = run?.permissions.get(input.request_id);
    if (!pending || run.controller.signal.aborted || !["allow_once", "deny"].includes(input.decision)) {
      return controlError("Permission request is invalid, expired or cancelled", input);
    }
    pending.finish({ outcome: input.decision === "allow_once" ? "allow_once" : "reject_once" });
  } else if (input.op === "shutdown") {
    void shutdown().then(() => process.exit(0));
  } else controlError("Unknown bridge operation", input);
}

let startupTimeout = 15000;
let permissionTimeout = 60000;
try {
  try { projectRoot = await fs.realpath(string(options["project-root"], "--project-root", 4096)); }
  catch { fail("INVALID_REQUEST", "--project-root must exist"); }
  if (!(await fs.stat(projectRoot)).isDirectory()) fail("INVALID_REQUEST", "--project-root must be a directory");
  stateDir = path.resolve(string(options["state-dir"], "--state-dir", 4096));
  runtimeDir = path.resolve(options["runtime-dir"] ?? assetDir);
  startupTimeout = milliseconds(options["startup-timeout-ms"], 15000, "--startup-timeout-ms");
  permissionTimeout = milliseconds(options["permission-timeout-ms"], 60000, "--permission-timeout-ms");
  if (options["test-agent-command-json"]) {
    if (options["test-mode"] !== "1") fail("INVALID_REQUEST", "Test command requires --test-mode 1");
    testCommand = JSON.parse(options["test-agent-command-json"]);
    if (!Array.isArray(testCommand) || !testCommand.length || testCommand.some(x => typeof x !== "string" || !x || x.includes("\0"))) fail("INVALID_REQUEST", "Test command must be argv strings");
  }
  const metadata = await readJson(path.join(runtimeDir, "node_modules", "acpx", "package.json"));
  if (metadata?.version !== "0.19.3") fail("RUNTIME_MISSING", "Install the pinned managed runtime acpx@0.19.3 before starting a run");
  runtimeApi = await import(pathToFileURL(path.join(runtimeDir, "node_modules", "acpx", "dist", "runtime.js")));
} catch (error) {
  startupError = error.code === "ERR_MODULE_NOT_FOUND" || error.code === "ENOENT"
    ? new BridgeError("RUNTIME_MISSING", "Managed runtime dependencies are missing; run install_runtime explicitly") : error;
}
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", line => {
  if (line.length > 4000000) return controlError("Protocol line exceeds 4000000 characters");
  try { command(JSON.parse(line)); } catch (error) { controlError(error.message); }
});
input.on("close", () => { void shutdown().then(() => process.exit(0)); });
process.on("SIGTERM", () => { void shutdown().then(() => process.exit(0)); });
process.on("SIGINT", () => { void shutdown().then(() => process.exit(0)); });
