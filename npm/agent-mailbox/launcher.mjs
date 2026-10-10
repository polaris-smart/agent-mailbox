import { createHash } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { access, chmod, mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawn } from 'node:child_process';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

export function selectTarget(manifest, platform = process.platform, arch = process.arch) {
  const key = `${platform}-${arch}`;
  const item = manifest.targets[key];
  if (!item || !/^[a-f0-9]{64}$/.test(item.sha256)) throw new Error(`No verified package for ${key}; see the GitHub Release for supported platforms.`);
  const expected = `https://github.com/polaris-smart/agent-mailbox/releases/download/v${manifest.version}/`;
  if (!item.url.startsWith(expected) || !/^Agent-Mailbox-[\w.\-]+\.(zip|tar\.gz)$/.test(item.url.slice(expected.length))) throw new Error('Invalid release asset URL.');
  return { ...item, key };
}

export function cacheRoot(platform = process.platform, env = process.env) {
  if (platform === 'win32') return join(env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'agent-mailbox', 'programs');
  if (platform === 'darwin') return join(homedir(), 'Library', 'Caches', 'agent-mailbox', 'programs');
  return join(env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'agent-mailbox', 'programs');
}

export function executableRelative(platform) {
  if (platform === 'darwin') return join('Agent Mailbox.app', 'Contents', 'MacOS', 'Agent Mailbox');
  return join('Agent Mailbox', platform === 'win32' ? 'Agent Mailbox.exe' : 'Agent Mailbox');
}

export async function downloadVerified(url, destination, expected, fetcher = fetch) {
  const response = await fetcher(url, { signal: AbortSignal.timeout(300000) });
  if (!response.ok || !response.body) throw new Error(`Download failed: HTTP ${response.status}`);
  const hash = createHash('sha256');
  const meter = new Transform({ transform(chunk, encoding, callback) { hash.update(chunk); callback(null, chunk); } });
  await pipeline(Readable.fromWeb(response.body), meter, createWriteStream(destination, { flags: 'wx', mode: 0o600 }));
  if (hash.digest('hex') !== expected) throw new Error('Release checksum mismatch; nothing has been installed.');
}

export function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { shell: false, ...options });
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`Extraction failed (${code ?? signal}).`)));
  });
}

export async function extract(archive, destination, platform) {
  if (platform === 'darwin') await run('/usr/bin/ditto', ['-x', '-k', archive, destination], { stdio: ['ignore', 'ignore', 'inherit'] });
  else await run(platform === 'win32' ? 'tar.exe' : 'tar', ['-xf', archive, '-C', destination], { stdio: ['ignore', 'ignore', 'inherit'] });
}

export async function ensureInstalled(manifest, options = {}) {
  const platform = options.platform || process.platform;
  const target = selectTarget(manifest, platform, options.arch || process.arch);
  const root = options.root || cacheRoot(platform);
  const final = join(root, `${manifest.version}-${target.key}-${target.sha256.slice(0, 12)}`);
  const binary = join(final, executableRelative(platform));
  async function ready() {
    try {
      const receipt = JSON.parse(await readFile(join(final, '.verified-release.json'), 'utf8'));
      if (receipt.sha256 !== target.sha256) return false;
      await access(binary);
      return true;
    } catch { return false; }
  }
  if (await ready()) return binary;
  await mkdir(root, { recursive: true, mode: 0o700 });
  const temporary = await mkdtemp(join(root, '.download-'));
  try {
    const archive = join(temporary, target.url.split('/').pop());
    const unpack = join(temporary, 'unpack');
    await mkdir(unpack, { mode: 0o700 });
    (options.log || console.error)(`Preparing agent-mailbox ${manifest.version} for ${target.key} from its verified GitHub Release…`);
    await (options.download || downloadVerified)(target.url, archive, target.sha256);
    await (options.extract || extract)(archive, unpack, platform);
    await access(join(unpack, executableRelative(platform)));
    if (platform !== 'win32') await chmod(join(unpack, executableRelative(platform)), 0o755);
    await writeFile(join(unpack, '.verified-release.json'), JSON.stringify({ version: manifest.version, sha256: target.sha256 }), { mode: 0o600 });
    try { await rename(unpack, final); }
    catch (error) { if (!(await ready())) throw error; }
    if (!(await ready())) throw new Error('Installed program verification failed.');
    return binary;
  } finally { await rm(temporary, { recursive: true, force: true }); }
}

export async function launch(binary, args, env = process.env) {
  const child = spawn(binary, args, { stdio: 'inherit', shell: false, env });
  const handlers = new Map(['SIGINT', 'SIGTERM'].map(signal => [signal, () => child.kill(signal)]));
  for (const [signal, handler] of handlers) process.on(signal, handler);
  try {
    return await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve(code ?? (signal === 'SIGINT' ? 130 : 143)));
    });
  } finally { for (const [signal, handler] of handlers) process.off(signal, handler); }
}
