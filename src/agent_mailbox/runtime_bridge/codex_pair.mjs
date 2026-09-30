// CLI and code-mode host must be one locked distribution: protocol v1 alone is
// insufficient. Codex 0.158 requires host timing fields older v1 hosts omit.
import fs from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";

const execute = promisify(execFile);
export const CODEX_VERSION = "0.158.0";
const targets = {
  "darwin-arm64": "aarch64-apple-darwin", "darwin-x64": "x86_64-apple-darwin",
  "linux-arm64": "aarch64-unknown-linux-musl", "linux-x64": "x86_64-unknown-linux-musl",
  "win32-arm64": "aarch64-pc-windows-msvc", "win32-x64": "x86_64-pc-windows-msvc",
};
function incompatible() {
  return Object.assign(new Error("The managed Codex CLI and code-mode host are missing or incompatible; prepare the pinned execution runtime"),
    { code: "RUNTIME_INCOMPATIBLE" });
}
function frame(value) {
  const payload = Buffer.from(JSON.stringify(value));
  const prefix = Buffer.alloc(4); prefix.writeUInt32LE(payload.length);
  return Buffer.concat([prefix, payload]);
}
export async function verifyCodeModeHost(host, timeoutMs = 5000) {
  // No model, account, MCP, or shell execution: one local JS arithmetic cell.
  const child = spawn(host, [], { stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
  return await new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0), settled = false;
    const finish = error => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      child.stdin.end(); child.kill();
      error ? reject(incompatible()) : resolve();
    };
    const timer = setTimeout(() => finish(true), timeoutMs);
    child.on("error", () => finish(true));
    child.on("exit", () => finish(true));
    child.stdin.on("error", () => finish(true));
    child.stdout.on("data", chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      while (!settled && buffer.length >= 4) {
        const size = buffer.readUInt32LE();
        if (size > 1024 * 1024) return finish(true);
        if (buffer.length < size + 4) return;
        let value;
        try { value = JSON.parse(buffer.subarray(4, size + 4)); } catch { return finish(true); }
        buffer = buffer.subarray(size + 4);
        if (value.type === "connection/ready") {
          if (value.selectedVersion !== 1) return finish(true);
          child.stdin.write(frame({ type: "operation/request", id: 1,
            request: { method: "session/open", sessionId: "mailbox-compatibility-check" } }));
        } else if (value.type === "operation/response" && value.id === 1) {
          if (value.result?.value?.type !== "session/ready") return finish(true);
          child.stdin.write(frame({ type: "operation/request", id: 2, request: {
            method: "session/execute", sessionId: "mailbox-compatibility-check", request: {
              tool_call_id: "compatibility-check", enabled_tools: [], source: "text(2+2)",
              yield_time_ms: 100, max_output_tokens: 10,
            } } }));
        } else if (value.type === "execute/initialResponse" && value.id === 2) {
          const result = value.result?.value?.Result;
          if (value.result?.status !== "ok" || !Number.isSafeInteger(result?.code_mode_host_duration_ns)
            || result.code_mode_host_duration_ns < 0 || result.error_text !== null
            || !result.content_items?.some(item => item.type === "input_text" && item.text === "4")) return finish(true);
          finish(false);
        } else if (value.type === "connection/rejected" || value.result?.status === "error") finish(true);
      }
    });
    child.stdin.write(frame({ type: "connection/hello", supportedVersions: [1],
      requiredCapabilities: [], optionalCapabilities: [] }));
  });
}
export async function resolveCodexPair(runtimeDir) {
  const platform = `${process.platform}-${process.arch}`;
  const target = targets[platform];
  if (!target) throw incompatible();
  const root = path.join(runtimeDir, "node_modules", "@openai");
  const nativePackage = path.join(root, `codex-${platform}`);
  const bin = path.join(nativePackage, "vendor", target, "bin");
  const suffix = process.platform === "win32" ? ".exe" : "";
  const cli = path.join(bin, `codex${suffix}`);
  const host = path.join(bin, `codex-code-mode-host${suffix}`);
  try {
    const [metadata, nativeMetadata] = await Promise.all([
      fs.readFile(path.join(root, "codex/package.json"), "utf8"),
      fs.readFile(path.join(nativePackage, "package.json"), "utf8"),
    ]);
    if (JSON.parse(metadata).version !== CODEX_VERSION
      || JSON.parse(nativeMetadata).version !== `${CODEX_VERSION}-${platform}`) throw incompatible();
    await Promise.all([fs.access(cli, constants.X_OK), fs.access(host, constants.X_OK)]);
    const result = await execute(cli, ["--version"], { timeout: 5000, maxBuffer: 4096, windowsHide: true });
    if (result.stdout.trim() !== `codex-cli ${CODEX_VERSION}`) throw incompatible();
    await verifyCodeModeHost(host);
  } catch { throw incompatible(); }
  return { source: "managed-runtime", version: CODEX_VERSION, path: cli, host_path: host };
}
