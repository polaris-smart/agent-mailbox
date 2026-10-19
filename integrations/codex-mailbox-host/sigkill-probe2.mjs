import {startCliReceiver} from './cli-on-demand.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {execSync} from 'node:child_process';
let root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'sigkill-p2-')));
const home = path.join(root, 'mail');
const endpoint = path.join(root, 'ipc', 'receiver.sock');
const stateDir = path.join(root, 'deliveries');
await fs.mkdir(home, {mode: 0o700});
await fs.mkdir(path.dirname(endpoint), {mode: 0o700});
await fs.mkdir(stateDir, {mode: 0o700});
const config = {binary: '/usr/local/bin/codex', codex_home: root, cwd: root,
  binding_id: 'probe-binding', employee_id: 'employee_probe', project_id: 'project_probe', thread_id: 'thread_probe'};
const recv = await startCliReceiver({home, socketPath: endpoint, stateDir, config});
console.log('receiver started');
// 找监听这个 socket 的进程并 SIGKILL
try {
  const out = execSync(`lsof ${endpoint} -t 2>/dev/null`).toString().trim();
  console.log('监听进程:', out || '(无)');
  for (const pid of out.split('\n').filter(Boolean)) {
    process.kill(parseInt(pid), 'SIGKILL');
    console.log('SIGKILL →', pid);
  }
} catch (e) { console.log('lsof 无结果:', e.message.slice(0, 60)); }
await new Promise(r => setTimeout(r, 800));
try { await fs.lstat(endpoint); console.log('socket 遗留: 是'); } catch { console.log('socket 遗留: 否'); }
try { await fs.lstat(path.join(stateDir, 'cli-active.lock')); console.log('lock 遗留: 是'); } catch { console.log('lock 遗留: 否'); }
// 尝试重启（走 removeDeadReceiverSocket 清理路径）
try {
  const recv2 = await startCliReceiver({home, socketPath: endpoint, stateDir, config});
  console.log('重启: 成功 ✓');
  await recv2.stop?.();
} catch (e) { console.log('重启: 失败 →', e.message.slice(0, 80)); }
console.log('PROBE-DONE');
