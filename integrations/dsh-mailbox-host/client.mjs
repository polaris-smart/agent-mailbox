// Executable hook transport. argv: home socket employee_id delivery_id.
import net from 'node:net';
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export async function deliver({ home, socketPath, employeeId, deliveryId, timeout = 8000 }) {
  if (!path.isAbsolute(home) || !path.isAbsolute(socketPath) ||
      !/^wake-[A-Za-z0-9_-]+$/.test(deliveryId)) throw new Error('invalid_hook_arguments');
  const envelope = JSON.parse(await fs.readFile(path.join(home, 'wake', 'wake-outbox', deliveryId + '.json'), 'utf8'));
  const binding = envelope.binding;
  if (envelope.employee_id !== employeeId || envelope.delivery_id !== deliveryId ||
      envelope.receipt_protocol !== 'agent-mailbox.host-receipt.v1' || !binding ||
      binding.employee_id !== employeeId) throw new Error('outbox_identity_mismatch');
  const expected = {
    protocol: envelope.receipt_protocol, delivery_id: deliveryId, employee_id: employeeId,
    message_ids: envelope.fresh, binding_id: binding.id, project_id: binding.project_id,
    host_kind: binding.host_kind, host_session_id: binding.host_session_id,
  };
  const receipt = await new Promise((resolve, reject) => {
    let connected = false, body = Buffer.alloc(0), settled = false;
    const socket = net.createConnection(socketPath);
    const finish = (error, result) => {
      if (settled) return; settled = true; socket.destroy();
      if (error) reject(error); else resolve(result);
    };
    socket.setTimeout(timeout, () => finish(new Error('host_delivery_unknown_timeout')));
    socket.on('connect', () => { connected = true; socket.write(JSON.stringify(envelope) + '\n'); });
    socket.on('error', error => {
      if (!connected && ['ENOENT', 'ECONNREFUSED'].includes(error.code)) {
        finish(null, { ...expected, status: 'not_received' });
      } else finish(new Error('host_delivery_unknown'));
    });
    socket.on('data', chunk => {
      body = Buffer.concat([body, chunk]);
      if (body.length > 65536) { finish(new Error('invalid_host_response')); return; }
      const end = body.indexOf(10); if (end < 0) return;
      try {
        const result = JSON.parse(body.subarray(0, end).toString('utf8'));
        if (!['received', 'not_received'].includes(result.status) ||
            Object.keys(expected).some(k => JSON.stringify(result[k]) !== JSON.stringify(expected[k]))) {
          throw new Error('host_receipt_mismatch');
        }
        finish(null, result);
      } catch { finish(new Error('host_delivery_unknown_invalid_receipt')); }
    });
    socket.on('end', () => { if (!settled) finish(new Error('host_delivery_unknown_closed')); });
  });
  const directory = path.join(home, 'wake', 'receipts');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 ||
      (process.getuid && stat.uid !== process.getuid())) throw new Error('unsafe_receipt_directory');
  const temporary = path.join(directory, '.' + randomUUID());
  try {
    const file = await fs.open(temporary, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(receipt)); await file.sync(); } finally { await file.close(); }
    await fs.rename(temporary, path.join(directory, deliveryId));
  } finally { await fs.unlink(temporary).catch(() => {}); }
  return receipt;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [home, socketPath, employeeId, deliveryId] = process.argv.slice(2);
  deliver({ home, socketPath, employeeId, deliveryId }).then(
    receipt => { process.exitCode = receipt.status === 'received' ? 0 : 2; },
    () => { console.error('Host delivery unconfirmed; no successful receipt written.'); process.exitCode = 1; },
  );
}
