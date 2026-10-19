// Internal preparation transaction. Trusted bridge supplies verified SDK/context;
// no browser-provided commands, plugin activation, session binding or model calls.
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const FILES = ['index.mjs', 'client.mjs', 'package.json', 'README.md'];
const LIMIT = 1024 * 1024;
const sha = raw => createHash('sha256').update(raw).digest('hex');
const fail = code => { throw new Error(code); };

async function directory(name, { source = false, create = false } = {}) {
  if (typeof name !== 'string' || !path.isAbsolute(name)) fail('absolute_directory_required');
  if (create) await fs.mkdir(name, { mode: 0o700 });
  const s = await fs.lstat(name);
  if (await fs.realpath(name) !== name || !s.isDirectory()
      || ![process.getuid(), ...(source ? [0] : [])].includes(s.uid)
      || (s.mode & (source ? 0o022 : 0o077))) fail('unsafe_directory');
  return name;
}

async function read(name, { source = false } = {}) {
  const file = await fs.open(name, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const s = await file.stat();
    if (!s.isFile() || s.nlink !== 1 || s.size > LIMIT
        || ![process.getuid(), ...(source ? [0] : [])].includes(s.uid)
        || (s.mode & (source ? 0o022 : 0o077))) fail('unsafe_file');
    const data = await file.readFile();
    if (data.length > LIMIT) fail('file_too_large');
    return data;
  } finally { await file.close(); }
}

async function syncDirectory(name) {
  const dir = await fs.open(name, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await dir.sync(); } finally { await dir.close(); }
}

async function writeNew(name, data) {
  const file = await fs.open(name, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
  try { await file.writeFile(data); await file.sync(); } finally { await file.close(); }
  await syncDirectory(path.dirname(name));
}

async function exists(name) {
  try { await fs.lstat(name); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function patchBytes(name) {
  try { return await read(name); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
const equal = (a, b) => a === null || b === null ? a === b : a.equals(b);

export class Preparation {
  constructor({ profile, source, prepare, readContext, atomic }) {
    // Dependencies come from the server-selected host SDK, never an HTTP payload.
    if (typeof prepare !== 'function' || typeof readContext !== 'function'
        || typeof atomic?.withFileLock !== 'function' || typeof atomic?.writeFileAtomic !== 'function') {
      fail('trusted_host_dependencies_required');
    }
    Object.assign(this, { profile, source, prepare, readContext, atomic });
    this.patch = path.join(profile, 'cordis.patch.yml');
    this.records = path.join(profile, 'mailbox-install-plans');
    this.target = path.join(profile, '.agent-mailbox-host');
  }

  async inputs() {
    await directory(this.profile);
    const manifest = await read(path.join(this.profile, 'package.json'));
    const bytes = await patchBytes(this.patch);
    let context;
    try { context = await this.readContext(); }
    catch { fail('host_context_unreadable'); }
    if (!Array.isArray(context.beforePatches) || !Array.isArray(context.afterPatches)) fail('explicit_profile_layers_required');
    const layers = { beforePatches: context.beforePatches, afterPatches: context.afterPatches };
    const contextSha = sha(JSON.stringify({ manifestSha: sha(manifest), ...layers }));
    if (!equal(bytes, await patchBytes(this.patch))
        || !manifest.equals(await read(path.join(this.profile, 'package.json')))) fail('configuration_changed_during_read');
    return { raw: bytes ?? Buffer.alloc(0), present: bytes !== null, layers, contextSha };
  }

  async components() {
    await directory(this.source, { source: true });
    const files = {};
    for (const name of FILES) files[name] = await read(path.join(this.source, name), { source: true });
    return files;
  }

  async locked(operation) {
    await directory(this.profile);
    // Official plugin-manager.change uses the manifest lock, not a patch-file lock.
    // Direct editors/third-party writers may not cooperate; hash checks detect observed
    // drift, but this is not an OS-level compare-and-swap against arbitrary writers.
    const manifest = path.join(this.profile, 'package.json');
    await read(manifest);
    const lock = manifest + '.lock';
    if (await exists(lock)) await read(lock); // refuse symlink/FIFO/wide lock before SDK access
    return this.atomic.withFileLock(manifest, operation, { waitMs: 500 });
  }

  async preview({ home, socketPath }) {
    return this.locked(async () => {
      await directory(home);
      await directory(path.dirname(socketPath));
      if (await fs.realpath(path.dirname(socketPath)) !== path.dirname(socketPath)
          || await exists(socketPath)) fail('new_private_socket_required');
      if (await exists(this.target)) fail('existing_component_requires_review');
      const { raw, present, layers, contextSha } = await this.inputs();
      const result = this.prepare(raw.toString('utf8'), { home, socketPath, profileDir: this.profile, ...layers });
      if (!Buffer.from(raw.toString('utf8')).equals(raw)) fail('invalid_utf8_configuration');
      const files = await this.components();
      if (!await exists(this.records)) await directory(this.records, { create: true });
      await directory(this.records);
      const id = 'plan_' + randomUUID().replaceAll('-', '');
      const folder = await directory(path.join(this.records, id), { create: true });
      const plan = { version: 1, id, profile: this.profile, expiresAt: Date.now() + 600000,
        home, socketPath, contextSha, beforeExists: present, beforeSha: sha(raw), afterSha: sha(result.text),
        files: Object.fromEntries(Object.entries(files).map(([k, v]) => [k, sha(v)])) };
      await writeNew(path.join(folder, 'before'), raw);
      await writeNew(path.join(folder, 'after'), result.text);
      await writeNew(path.join(folder, 'plan.json'), JSON.stringify(plan));
      return { plan_id: id, state: 'preview', will_activate: false,
        session_verified: false, delivery_verified: false };
    });
  }

  async load(id) {
    if (typeof id !== 'string' || !/^plan_[a-f0-9]{32}$/.test(id)) fail('invalid_plan_id');
    await directory(this.profile); await directory(this.records);
    const folder = await directory(path.join(this.records, id));
    const plan = JSON.parse((await read(path.join(folder, 'plan.json'))).toString('utf8'));
    if (plan.version !== 1 || plan.id !== id || plan.profile !== this.profile
        || typeof plan.beforeExists !== 'boolean') fail('plan_scope_mismatch');
    const before = await read(path.join(folder, 'before'));
    const after = await read(path.join(folder, 'after'));
    if (sha(before) !== plan.beforeSha || sha(after) !== plan.afterSha) fail('plan_content_changed');
    return { folder, plan, before, after };
  }

  async replace(expected, next) {
    if (!equal(await patchBytes(this.patch), expected)) fail('configuration_changed');
    if (next === null) {
      await fs.unlink(this.patch);
    } else if (expected === null) {
      // A fresh file must never overwrite a concurrently created configuration.
      const temp = this.patch + '.' + randomUUID() + '.prepare';
      await writeNew(temp, next);
      try { await fs.link(temp, this.patch); } finally { await fs.unlink(temp); }
    } else {
      await this.atomic.writeFileAtomic(this.patch, next, { mode: 0o600 });
      const file = await fs.open(this.patch, constants.O_RDONLY | constants.O_NOFOLLOW);
      try { await file.sync(); } finally { await file.close(); }
    }
    await syncDirectory(this.profile);
    if (!equal(await patchBytes(this.patch), next)) fail('configuration_write_unconfirmed');
  }

  async apply(id, { confirmed = false } = {}) {
    if (confirmed !== true) fail('explicit_confirmation_required');
    return this.locked(async () => {
      const { folder, plan, before, after } = await this.load(id);
      if (await exists(path.join(folder, 'removed.json'))) fail('plan_closed');
      if (Date.now() > plan.expiresAt) fail('plan_expired');
      if (!equal(await patchBytes(this.patch), plan.beforeExists ? before : null)) fail('configuration_changed');
      const current = await this.inputs();
      if (!current.raw.equals(before) || current.present !== plan.beforeExists
          || current.contextSha !== plan.contextSha) fail('configuration_changed');
      await directory(plan.home); await directory(path.dirname(plan.socketPath));
      if (await exists(plan.socketPath) || await exists(this.target)) fail('existing_component_requires_review');
      const recomputed = this.prepare(before.toString('utf8'), { home: plan.home, socketPath: plan.socketPath,
        profileDir: this.profile, ...current.layers });
      if (sha(recomputed.text) !== plan.afterSha) fail('plan_revalidation_failed');
      const files = await this.components();
      if (JSON.stringify(Object.fromEntries(Object.entries(files).map(([k,v]) => [k,sha(v)]))) !== JSON.stringify(plan.files)) {
        fail('component_changed');
      }
      // Durable one-shot intent; unknown completion never replays the write.
      await writeNew(path.join(folder, 'started.json'), JSON.stringify({ startedAt: Date.now() }));
      try {
        await directory(this.target, { create: true });
        const s = await fs.lstat(this.target);
        await writeNew(path.join(folder, 'publication.json'), JSON.stringify({ dev: s.dev, ino: s.ino }));
        for (const [name, bytes] of Object.entries(files)) await writeNew(path.join(this.target, name), bytes);
        const recheck = await this.inputs();
        if (!recheck.raw.equals(before) || recheck.present !== plan.beforeExists
            || recheck.contextSha !== plan.contextSha) fail('configuration_changed');
        await this.replace(plan.beforeExists ? before : null, after);
        await writeNew(path.join(folder, 'prepared.json'), JSON.stringify({ afterSha: plan.afterSha }));
      } catch {
        fail('preparation_incomplete_inspect_record'); // retain data/evidence, no blind rollback
      }
      return { plan_id: id, state: 'prepared_disabled', will_activate: false,
        session_verified: false, delivery_verified: false };
    });
  }

  async reconcile(id, { confirmed = false } = {}) {
    if (confirmed !== true) fail('explicit_confirmation_required');
    return this.locked(async () => {
      const { folder, plan, after } = await this.load(id);
      if (await exists(path.join(folder, 'removed.json'))
          || await exists(path.join(folder, 'removing.json'))) fail('plan_closed_or_removing');
      // Only acknowledge an already complete disabled publication. Never retry
      // the installation, fill partial files, or infer success from an intent.
      await read(path.join(folder, 'started.json'));
      await directory(plan.home); await directory(path.dirname(plan.socketPath));
      if (await exists(plan.socketPath)) fail('receiver_endpoint_requires_inspection');
      const current = await this.inputs();
      if (!current.present || !current.raw.equals(after)
          || current.contextSha !== plan.contextSha) fail('configuration_changed');
      await this.verifyPublication(folder, plan);
      if (!equal(await patchBytes(this.patch), after)) fail('configuration_changed');
      const record = path.join(folder, 'prepared.json');
      if (!await exists(record)) {
        await writeNew(record, JSON.stringify({ afterSha: plan.afterSha, reconciledAt: Date.now() }));
      } else {
        const prepared = JSON.parse((await read(record)).toString('utf8'));
        if (prepared.afterSha !== plan.afterSha) fail('preparation_record_changed');
      }
      return { plan_id: id, state: 'prepared_disabled', will_activate: false,
        session_verified: false, delivery_verified: false };
    });
  }

  async remove(id, { confirmed = false } = {}) {
    if (confirmed !== true) fail('explicit_confirmation_required');
    return this.locked(async () => {
      const { folder, plan, before, after } = await this.load(id);
      if (await exists(path.join(folder, 'removed.json'))) return { plan_id: id, state: 'removed' };
      if (!await exists(path.join(folder, 'started.json'))) {
        await writeNew(path.join(folder, 'removed.json'), '{"cancelled_before_apply":true}');
        return { plan_id: id, state: 'removed' };
      }
      if (!await exists(path.join(folder, 'prepared.json'))) fail('incomplete_preparation_requires_inspection');
      if (await exists(path.join(folder, 'removing.json'))) fail('incomplete_removal_requires_inspection');
      if (!equal(await patchBytes(this.patch), after)) fail('configuration_changed');
      const current = await this.inputs();
      if (!current.raw.equals(after) || current.contextSha !== plan.contextSha) fail('configuration_changed');
      // Never remove an activated/modified/replaced plugin, or discard an external edit.
      await this.verifyPublication(folder, plan);
      await writeNew(path.join(folder, 'removing.json'), JSON.stringify({ startedAt: Date.now() }));
      try {
        await this.replace(after, plan.beforeExists ? before : null);
        // Move, do not recursively delete: retain this exact publication for diagnosis.
        await fs.rename(this.target, path.join(folder, 'removed-component'));
        await syncDirectory(this.profile); await syncDirectory(folder);
        await writeNew(path.join(folder, 'removed.json'), '{"removed":true}');
      } catch { fail('removal_incomplete_inspect_record'); }
      return { plan_id: id, state: 'removed' };
    });
  }

  async verifyPublication(folder, plan) {
    await directory(this.target);
    const pub = JSON.parse((await read(path.join(folder, 'publication.json'))).toString());
    const s = await fs.lstat(this.target);
    if (s.dev !== pub.dev || s.ino !== pub.ino) fail('component_ownership_changed');
    if (JSON.stringify((await fs.readdir(this.target)).sort()) !== JSON.stringify([...FILES].sort())) fail('component_changed');
    for (const name of FILES) if (sha(await read(path.join(this.target, name))) !== plan.files[name]) fail('component_changed');
  }

  async status(id) {
    // Observation only: no lock acquisition, recovery writes, activation or retry.
    const { folder, plan, after } = await this.load(id);
    let state;
    if (await exists(path.join(folder, 'removed.json'))) state = 'removed';
    else if (await exists(path.join(folder, 'removing.json'))) state = 'removal_unconfirmed';
    else if (await exists(path.join(folder, 'prepared.json'))) {
      const current = await this.inputs();
      if (!current.raw.equals(after) || current.contextSha !== plan.contextSha) state = 'configuration_changed';
      else {
        await this.verifyPublication(folder, plan);
        state = equal(await patchBytes(this.patch), after) ? 'prepared_disabled' : 'configuration_changed';
      }
    } else if (await exists(path.join(folder, 'started.json'))) state = 'preparation_unconfirmed';
    else state = Date.now() > plan.expiresAt ? 'expired' : 'preview';
    return { plan_id: id, state, will_activate: false, session_verified: false, delivery_verified: false };
  }
}
