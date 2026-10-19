// Preserve Desktop's backend and protocol; expose that same backend on a private Unix socket.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import { connect } from './socket.mjs';
export { connect } from './socket.mjs';

export function notifyHostIdle(base, threadId) {
  if (typeof threadId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(threadId)) {
    return Promise.resolve(false);
  }
  return new Promise(resolve => {
    const socket = net.createConnection(path.join(base, 'idle.sock'));
    let settled = false;
    let response = '';
    const finish = ok => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(9000, () => finish(false));
    socket.once('error', () => finish(false));
    socket.once('end', () => finish(false));
    socket.on('data', chunk => {
      response += chunk.toString('utf8');
      if (response.length > 512) { finish(false); return; }
      const end = response.indexOf('\n');
      if (end < 0) return;
      try {
        const answer = JSON.parse(response.slice(0, end));
        finish(answer?.protocol === 'agent-mailbox.host-activity.v1' &&
          answer.thread_id === threadId && answer.processed === true &&
          Object.keys(answer).length === 3);
      } catch { finish(false); }
    });
    socket.once('connect', () => {
      socket.write(JSON.stringify({protocol:'agent-mailbox.host-activity.v1',
        kind:'turn_completed',thread_id:threadId})+'\n');
    });
  });
}

async function traceIdleProbe(base, threadId, stage, processed = null) {
  // Opt-in, owner-private diagnostic for a single live acceptance run.
  // Never record a prompt, message body, socket payload or raw thread ID.
  let enabled;
  try { enabled = await fs.lstat(path.join(base, '.idle-probe')); }
  catch { return; }
  if (!enabled.isFile() || enabled.isSymbolicLink() || enabled.uid !== process.getuid() ||
      (enabled.mode & 0o077)) return;
  const record = { at: new Date().toISOString(), stage,
    thread_sha256: createHash('sha256').update(threadId).digest('hex'), processed };
  try { await fs.writeFile(path.join(base, '.idle-probe-result.json'),
    JSON.stringify(record) + '\n', { mode: 0o600 }); }
  catch { /* Diagnostics must never interfere with the Desktop protocol. */ }
}

export function serverArguments(args) {
  const index = args.indexOf('app-server');
  if (index < 0 || ['--help', '-h', '--version'].some(v => args.includes(v))) return null;
  const tail = args.slice(index + 1);
  if (tail.some(v => ['proxy', 'daemon', 'generate-ts', 'generate-json-schema', 'help'].includes(v))) return null;
  const result = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--stdio') continue;
    if (args[i] === '--listen') {
      if (args[++i] !== 'stdio://') return null;
      continue;
    }
    if (args[i].startsWith('--listen=')) {
      if (args[i] !== '--listen=stdio://') return null;
      continue;
    }
    result.push(args[i]);
  }
  return result;
}

export async function launch({ binary, args, base, mailboxHome, input = process.stdin, output = process.stdout }) {
  if (!path.isAbsolute(binary) || path.resolve(binary) === fileURLToPath(import.meta.url)) {
    throw new Error('explicit_original_codex_binary_required');
  }
  const originalArgs = serverArguments(args);
  if (originalArgs === null) {
    return await new Promise((resolve, reject) => {
      const child = spawn(binary, args, { stdio: 'inherit' });
      child.once('error', reject); child.once('exit', code => resolve(code ?? 1));
    });
  }
  if (process.platform === 'win32') throw new Error('windows_transport_not_implemented');
  if (mailboxHome) mailboxHome = await fs.realpath(mailboxHome);
  await fs.mkdir(base, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(base);
  if (!stat.isDirectory() || stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o077)) {
    throw new Error('endpoint_directory_must_be_owner_private');
  }
  const identity = `${process.pid}-${randomBytes(6).toString('hex')}`;
  const socketPath = path.join(base, identity + '.sock');
  const manifest = path.join(base, identity + '.json');
  if (Buffer.byteLength(socketPath) > 100) throw new Error('endpoint_path_too_long');
  let server, ws, stopReceiver, stopping = false, stopPromise;
  const stop = () => stopPromise ??= (async () => {
    stopping = true;
    await stopReceiver?.();
    ws?.terminate();
    if (server?.pid && server.exitCode === null && server.signalCode === null) {
      const exited = new Promise(resolve => server.once('exit', resolve));
      server.kill('SIGTERM');
      const force = setTimeout(() => server.kill('SIGKILL'), 5000);
      await exited; clearTimeout(force);
    }
    await fs.rm(manifest, { force: true });
    await fs.rm(socketPath, { force: true }); // Random name allocated by this launcher only.
  })();
  const onSignal = () => { void stop(); };
  process.on('SIGTERM', onSignal); process.on('SIGINT', onSignal);
  try {
    server = spawn(binary, [...originalArgs, '--listen', 'unix://' + socketPath], {
      stdio: ['ignore', 'ignore', 'inherit'],
    });
    let startupError;
    server.on('error', error => { startupError = error; });
    // Startup readiness only; no recurring mail polling or scheduled prompts.
    const deadline = Date.now() + 15000;
    while (!(await fs.stat(socketPath).catch(() => null))) {
      if (startupError) throw startupError;
      if (stopping || server.exitCode !== null || server.signalCode !== null || Date.now() > deadline) {
        throw new Error('original_app_server_not_ready');
      }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    await fs.chmod(socketPath, 0o600);
    ws = await connect(socketPath);
    let notificationSocket;
    if(mailboxHome) {
      notificationSocket=path.join(base,identity+'.mail.sock');
      const {startNativeReceiver}=await import('./native.mjs');
      stopReceiver=await startNativeReceiver({home:mailboxHome,socketPath:notificationSocket,
        backendSocket:socketPath,stateDir:path.join(base,'deliveries')});
    }
    await fs.writeFile(manifest, JSON.stringify({ version: 1, launcher_pid: process.pid,
      server_pid: server.pid, socket: socketPath, notification_socket:notificationSocket,
      binary, mailbox_home:mailboxHome, created_at: new Date().toISOString() }),
    { mode: 0o600, flag: 'wx' });
    if(stopping)throw new Error('launcher_stopped_during_startup');
    return await new Promise((resolve, reject) => {
      let buffer = '', ended = false;
      const finish = (error) => {
        if (ended) return; ended = true;
        input.removeListener('data', onData); input.removeListener('end', onEnd);
        input.pause();
        output.removeListener('drain', onDrain);
        output.removeListener('error', finish);
        error ? reject(error) : resolve(0);
      };
      const onData = chunk => {
        buffer += chunk;
        let at;
        while ((at = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
          if (line.trim()) ws.send(line);
        }
      };
      const onEnd = () => { ws.close(); finish(); };
      const onDrain = () => ws.resume();
      input.setEncoding('utf8'); input.on('data', onData); input.once('end', onEnd);
      input.resume();
      let forwarding = Promise.resolve();
      ws.on('message', (data, binaryFrame) => {
        forwarding = forwarding.then(async () => {
          if (ended) return;
          if (binaryFrame) throw new Error('unexpected_binary_frame');
          let forwarded = false;
          if (mailboxHome && data.includes('turn/completed')) {
            try {
              const event = JSON.parse(data.toString('utf8'));
              if (event.method === 'turn/completed') {
                // Let Desktop finish its turn before probing admission. Some
                // backends report the thread busy until this frame is forwarded.
                // If Desktop starts another turn first, the receiver's own
                // idle check defers the durable mail instead of steering it.
                const threadId = event.params?.threadId;
                if (!output.write(data.toString('utf8') + '\n')) ws.pause();
                forwarded = true;
                if (typeof threadId === 'string') {
                  await traceIdleProbe(base, threadId, 'observed');
                }
                const processed = await notifyHostIdle(base, threadId);
                if (typeof threadId === 'string') {
                  await traceIdleProbe(base, threadId, 'checked', processed);
                }
              }
            } catch { /* An unrelated frame must still reach Desktop unchanged. */ }
          }
          if (!forwarded && !output.write(data.toString('utf8') + '\n')) ws.pause();
        }).catch(finish);
      });
      output.on('drain', onDrain); output.once('error', finish);
      ws.once('error', finish); ws.once('close', () => finish());
      server.once('exit', () => finish());
    });
  } finally {
    await stop();
    // A stop can race asynchronous receiver creation or manifest publication.
    // Close any receiver that finished starting after stop() first inspected it.
    await stopReceiver?.();
    await fs.rm(manifest, {force:true});
    await fs.rm(socketPath, {force:true});
    process.removeListener('SIGTERM', onSignal); process.removeListener('SIGINT', onSignal);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const binary = process.env.AGENT_MAILBOX_CODEX_BINARY;
  const base = process.env.AGENT_MAILBOX_CODEX_SOCKET_DIR ?? path.join(os.homedir(), '.codex', 'mailbox-host');
  try { process.exitCode = await launch({ binary, args: process.argv.slice(2), base,
    mailboxHome:process.env.AGENT_MAILBOX_CODEX_MAILBOX_HOME }); }
  catch (error) { console.error('mailbox Codex transport:', error.message); process.exitCode = 1; }
}
