import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { planner, ENTRY_ID } from '../integrations/dsh-mailbox-host/patch-plan.mjs';
import { Preparation } from '../integrations/dsh-mailbox-host/setup.mjs';

const sdk = process.env.DSH_TEST_SDK;
if (!sdk || !path.isAbsolute(sdk)) throw new Error('explicit_verified_dsh_sdk_required');
const load = name => import(pathToFileURL(path.join(sdk, name)).href);
const yaml = (await load('js-yaml/index.js')).default;
const include = await load('@deepseek-ai/cordis-plugin-include/lib/index.js');
const cst = await load('yaml/dist/index.js');
const boot = await load('@deepseek-ai/dsh-app-boot/lib/index.js');
const atomic = await load('@deepseek-ai/dsh-atomic-write/lib/index.js');
const prepare = planner({ yaml, ...include, cst });
const source = fileURLToPath(new URL('../integrations/dsh-mailbox-host', import.meta.url));
const anchor = path.join(sdk, '@deepseek-ai/dsh/package.json');
const files = ['index.mjs', 'client.mjs', 'package.json', 'README.md'];

async function fixture(t, { absent = false, writer = atomic } = {}) {
  const base = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-prep-')));
  t.after(() => fs.rm(base, { recursive: true, force: true }));
  const profile = path.join(base, 'profile'); const home = path.join(base, 'home');
  await fs.mkdir(profile, { mode: 0o700 }); await fs.mkdir(home, { mode: 0o700 });
  const patch = path.join(profile, 'cordis.patch.yml');
  const before = '# unrelated retained\n[]\n';
  await fs.writeFile(path.join(profile, 'package.json'), JSON.stringify({
    private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } },
  }), { mode: 0o600 });
  if (!absent) await fs.writeFile(patch, before, { mode: 0o600 });
  let afterPatches = [];
  const readContext = () => {
    const snapshot = boot.loadProfileDirectory('mailbox-test', profile, anchor);
    assert.deepEqual(snapshot.skippedBundles, []);
    return { beforePatches: snapshot.layers.flatMap(x => x.patches), afterPatches };
  };
  const setup = new Preparation({ profile, source, prepare, readContext, atomic: writer });
  const args = { home, socketPath: path.join(base, 'receiver.sock') };
  const preview = () => setup.preview(args);
  const rows = () => { const data = boot.loadProfileDirectory('mailbox-test', profile, anchor);
    return boot.composeEntries([...data.layers.map(x => x.patches), data.patches], assert.fail); };
  return { base, profile, home, patch, before, setup, args, preview, rows,
    setAfter: value => { afterPatches = value; } };
}

const notExists = async p => assert.rejects(fs.lstat(p), { code: 'ENOENT' });

async function uncertainPublication(t, { committed = true } = {}) {
  let writes = 0;
  const writer = { ...atomic, writeFileAtomic: async (...args) => {
    writes++;
    if (committed) await atomic.writeFileAtomic(...args);
    throw new Error('synthetic lost installation result');
  } };
  const f = await fixture(t, { writer }); const p = await f.preview();
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /preparation_incomplete/);
  return { ...f, id: p.plan_id, folder: path.join(f.setup.records, p.plan_id), writes: () => writes };
}

test('explicit reconciliation acknowledges committed disabled state without replay, even after expiry', async t => {
  const f = await uncertainPublication(t);
  assert.equal((await f.setup.status(f.id)).state, 'preparation_unconfirmed');
  await assert.rejects(f.setup.reconcile(f.id), /explicit_confirmation/);
  const record = path.join(f.folder, 'plan.json');
  const plan = JSON.parse(await fs.readFile(record)); plan.expiresAt = 0;
  await fs.writeFile(record, JSON.stringify(plan));
  const before = await fs.stat(f.patch);
  const result = await f.setup.reconcile(f.id, { confirmed: true });
  assert.equal(result.state, 'prepared_disabled'); assert.equal(result.will_activate, false);
  assert.equal(result.session_verified, false); assert.equal(result.delivery_verified, false);
  assert.equal(f.rows().find(x => x.id === ENTRY_ID).disabled, true);
  const receipt = await fs.readFile(path.join(f.folder, 'prepared.json'));
  await f.setup.reconcile(f.id, { confirmed: true });
  assert.deepEqual(await fs.readFile(path.join(f.folder, 'prepared.json')), receipt);
  assert.equal((await fs.stat(f.patch)).mtimeMs, before.mtimeMs);
  assert.equal(f.writes(), 1); await notExists(f.args.socketPath);
  // Once independently reconciled, the existing guarded removal works again.
  f.setup.atomic = atomic;
  await f.setup.remove(f.id, { confirmed: true });
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before);
});

test('uncommitted installation cannot be reconciled or silently completed', async t => {
  const f = await uncertainPublication(t, { committed: false });
  await assert.rejects(f.setup.reconcile(f.id, { confirmed: true }), /configuration_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before); assert.equal(f.writes(), 1);
  await notExists(path.join(f.folder, 'prepared.json'));
});

for (const [label, mutate, error] of [
  ['external patch', f => fs.appendFile(f.patch, '# external\n'), /configuration_changed/],
  ['overlay drift', f => f.setAfter([{ id: 'other', disabled: true }]), /configuration_changed/],
  ['incomplete component', f => fs.unlink(path.join(f.setup.target, 'client.mjs')), /component_changed/],
  ['changed component', f => fs.appendFile(path.join(f.setup.target, 'index.mjs'), '// edit'), /component_changed/],
  ['existing endpoint', f => fs.writeFile(f.args.socketPath, 'do not touch', { mode: 0o600 }), /receiver_endpoint/],
  ['removal intent', f => fs.writeFile(path.join(f.folder, 'removing.json'), '{}', { mode: 0o600 }), /plan_closed_or_removing/],
  ['replaced directory', async f => {
    await fs.rename(f.setup.target, f.setup.target + '.old');
    await fs.mkdir(f.setup.target, { mode: 0o700 });
    for (const name of files) await fs.copyFile(path.join(source, name), path.join(f.setup.target, name));
  }, /component_ownership_changed/],
]) test(`reconciliation refuses ${label} and preserves unknown outcome`, async t => {
  const f = await uncertainPublication(t); await mutate(f);
  const patch = await fs.readFile(f.patch);
  await assert.rejects(f.setup.reconcile(f.id, { confirmed: true }), error);
  assert.deepEqual(await fs.readFile(f.patch), patch); assert.equal(f.writes(), 1);
  await notExists(path.join(f.folder, 'prepared.json'));
});

test('preview alone is never evidence of an installed component', async t => {
  const f = await fixture(t); const p = await f.preview();
  await assert.rejects(f.setup.reconcile(p.plan_id, { confirmed: true }), { code: 'ENOENT' });
  await notExists(f.setup.target);
  assert.equal((await f.setup.status(p.plan_id)).state, 'preview');
});

test('concurrent explicit reconciliation creates one completion and performs no second install', async t => {
  const f = await uncertainPublication(t);
  const results = await Promise.all([1, 2].map(() => f.setup.reconcile(f.id, { confirmed: true })));
  assert.ok(results.every(r => r.state === 'prepared_disabled')); assert.equal(f.writes(), 1);
  assert.equal((await f.setup.status(f.id)).state, 'prepared_disabled');
});

test('changed completion receipt is refused rather than silently overwritten', async t => {
  const f = await uncertainPublication(t); await f.setup.reconcile(f.id, { confirmed: true });
  const record = path.join(f.folder, 'prepared.json'); const changed = '{"afterSha":"changed"}';
  await fs.writeFile(record, changed);
  await assert.rejects(f.setup.reconcile(f.id, { confirmed: true }), /preparation_record_changed/);
  assert.equal(await fs.readFile(record, 'utf8'), changed);
});

test('official profile preparation installs disabled component; removal restores exact prior bytes', async t => {
  const f = await fixture(t); const beforeRows = f.rows();
  const p = await f.preview();
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before);
  await notExists(f.setup.target);
  assert.equal(p.will_activate, false);
  const result = await f.setup.apply(p.plan_id, { confirmed: true });
  assert.equal(result.state, 'prepared_disabled'); assert.equal(result.session_verified, false);
  const rows = f.rows();
  assert.equal(rows.filter(x => x.id === ENTRY_ID).length, 1);
  assert.equal(rows.find(x => x.id === ENTRY_ID).disabled, true);
  assert.deepEqual(rows.filter(x => x.id !== ENTRY_ID), beforeRows);
  for (const name of files) assert.deepEqual(await fs.readFile(path.join(f.setup.target, name)), await fs.readFile(path.join(source, name)));
  await notExists(f.args.socketPath);
  await f.setup.remove(p.plan_id, { confirmed: true });
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before);
  await notExists(f.setup.target);
  assert.equal((await f.setup.remove(p.plan_id, { confirmed: true })).state, 'removed');
});

test('missing patch remains missing after prepare/remove, distinct from existing empty file', async t => {
  const f = await fixture(t, { absent: true }); const p = await f.preview();
  await notExists(f.patch);
  await f.setup.apply(p.plan_id, { confirmed: true });
  assert.equal(f.rows().find(x => x.id === ENTRY_ID).disabled, true);
  await f.setup.remove(p.plan_id, { confirmed: true }); await notExists(f.patch);
});

test('confirmation required for mutation; preview cancellation touches no component', async t => {
  const f = await fixture(t); const p = await f.preview();
  await assert.rejects(f.setup.apply(p.plan_id), /explicit_confirmation/);
  await assert.rejects(f.setup.remove(p.plan_id), /explicit_confirmation/);
  await f.setup.remove(p.plan_id, { confirmed: true });
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /plan_closed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before); await notExists(f.setup.target);
});

test('existing receiver path never overwritten', async t => {
  const f = await fixture(t); await fs.mkdir(f.setup.target, { mode: 0o700 });
  await fs.writeFile(path.join(f.setup.target, 'user'), 'keep');
  await assert.rejects(f.preview(), /existing_component/);
  assert.equal(await fs.readFile(path.join(f.setup.target, 'user'), 'utf8'), 'keep');
});

test('external config edit before apply refuses without publication', async t => {
  const f = await fixture(t); const p = await f.preview();
  const changed = f.before + '# external change\n'; await fs.writeFile(f.patch, changed);
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /configuration_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), changed); await notExists(f.setup.target);
});

test('manifest and overlay changes invalidate a plan', async t => {
  const f = await fixture(t); const p = await f.preview();
  f.setAfter([{ id: 'irrelevant', disabled: true }]);
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /configuration_changed/);
  f.setAfter([]);
  await fs.appendFile(path.join(f.profile, 'package.json'), '\n');
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /configuration_changed/);
  await notExists(f.setup.target);
});

test('external edit after apply refuses removal, preserving bytes and component', async t => {
  const f = await fixture(t); const p = await f.preview(); await f.setup.apply(p.plan_id, { confirmed: true });
  const changed = await fs.readFile(f.patch, 'utf8') + '# unrelated new edit\n';
  await fs.writeFile(f.patch, changed);
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /configuration_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), changed); assert.ok((await fs.stat(f.setup.target)).isDirectory());
});

test('activation is an external edit and cannot be undone by preparation removal', async t => {
  const f = await fixture(t); const p = await f.preview(); await f.setup.apply(p.plan_id, { confirmed: true });
  const text = (await fs.readFile(f.patch, 'utf8')).replace('disabled: true', 'disabled: false');
  await fs.writeFile(f.patch, text);
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /configuration_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), text);
});

test('component edits and extra files block removal', async t => {
  const f = await fixture(t); const p = await f.preview(); await f.setup.apply(p.plan_id, { confirmed: true });
  await fs.appendFile(path.join(f.setup.target, 'index.mjs'), '// external\n');
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /component_changed/);
  await fs.writeFile(path.join(f.setup.target, 'external.txt'), 'keep');
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /component_changed/);
});

test('unknown write outcome retained, never auto-rolled back or replayed', async t => {
  let writes = 0;
  const writer = { ...atomic, writeFileAtomic: async (...args) => {
    writes++; await atomic.writeFileAtomic(...args); throw new Error('synthetic failure after commit');
  } };
  const f = await fixture(t, { writer }); const p = await f.preview();
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /preparation_incomplete/);
  assert.equal(f.rows().find(x => x.id === ENTRY_ID).disabled, true);
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }));
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /incomplete_preparation/);
  assert.equal(writes, 1);
});

test('two plans serialize through official manifest lock; only one publishes', async t => {
  const f = await fixture(t); const a = await f.preview(); const b = await f.preview();
  const results = await Promise.allSettled([a, b].map(p => f.setup.apply(p.plan_id, { confirmed: true })));
  assert.equal(results.filter(x => x.status === 'fulfilled').length, 1);
  assert.equal(results.filter(x => x.status === 'rejected').length, 1);
  assert.equal(f.rows().filter(x => x.id === ENTRY_ID).length, 1);
});

test('traversal and forged plan scope refused', async t => {
  const f = await fixture(t); const p = await f.preview();
  for (const name of ['../outside', '/tmp/plan', 'plan_']) await assert.rejects(f.setup.load(name), /invalid_plan_id/);
  const file = path.join(f.setup.records, p.plan_id, 'plan.json');
  const data = JSON.parse(await fs.readFile(file)); data.profile = '/private/tmp/unrelated';
  await fs.writeFile(file, JSON.stringify(data));
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /plan_scope_mismatch/);
});

test('symlink, hardlink and wide patch files are refused', async t => {
  const f = await fixture(t); const other = path.join(f.base, 'other');
  await fs.writeFile(other, f.before, { mode: 0o600 });
  await fs.unlink(f.patch); await fs.symlink(other, f.patch); await assert.rejects(f.preview());
  await fs.unlink(f.patch); await fs.link(other, f.patch); await assert.rejects(f.preview(), /unsafe_file/);
  await fs.unlink(f.patch); await fs.writeFile(f.patch, f.before, { mode: 0o644 });
  await assert.rejects(f.preview(), /unsafe_file/);
});

test('existing socket and symlinked lock refused without following or deleting', async t => {
  const f = await fixture(t);
  await fs.writeFile(f.args.socketPath, 'other owner', { mode: 0o600 });
  await assert.rejects(f.preview(), /new_private_socket/);
  await fs.unlink(f.args.socketPath);
  const lock = path.join(f.profile, 'package.json.lock'); const target = path.join(f.base, 'do-not-touch');
  await fs.writeFile(target, '2147483647\n', { mode: 0o600 }); await fs.symlink(target, lock);
  await assert.rejects(f.preview());
  assert.equal(await fs.readFile(target, 'utf8'), '2147483647\n'); assert.ok((await fs.lstat(lock)).isSymbolicLink());
});

test('new empty patch created after absent preview does not count as unchanged', async t => {
  const f = await fixture(t, { absent: true }); const p = await f.preview();
  await fs.writeFile(f.patch, '', { mode: 0o600 });
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /configuration_changed/);
  await notExists(f.setup.target);
});

test('status reads stable preparation and changed config without writing records', async t => {
  const f = await fixture(t); const p = await f.preview();
  assert.equal((await f.setup.status(p.plan_id)).state, 'preview');
  await f.setup.apply(p.plan_id, { confirmed: true });
  const folder = path.join(f.setup.records, p.plan_id);
  const names = await fs.readdir(folder);
  const before = await Promise.all(names.map(n => fs.stat(path.join(folder, n))));
  assert.equal((await f.setup.status(p.plan_id)).state, 'prepared_disabled');
  assert.deepEqual((await Promise.all(names.map(n => fs.stat(path.join(folder, n))))).map(s => s.mtimeMs), before.map(s => s.mtimeMs));
  await fs.appendFile(f.patch, '# user edit\n');
  assert.equal((await f.setup.status(p.plan_id)).state, 'configuration_changed');
});

test('expired plan and changed source refuse without changing configuration', async t => {
  const f = await fixture(t); const p = await f.preview();
  const record = path.join(f.setup.records, p.plan_id, 'plan.json');
  const plan = JSON.parse(await fs.readFile(record)); plan.expiresAt = 0;
  await fs.writeFile(record, JSON.stringify(plan));
  assert.equal((await f.setup.status(p.plan_id)).state, 'expired');
  await assert.rejects(f.setup.apply(p.plan_id, { confirmed: true }), /plan_expired/);
  const local = path.join(f.base, 'source'); await fs.mkdir(local, { mode: 0o700 });
  for (const name of files) await fs.copyFile(path.join(source, name), path.join(local, name));
  f.setup.source = local;
  const p2 = await f.preview(); await fs.appendFile(path.join(local, 'index.mjs'), '// changed\n');
  await assert.rejects(f.setup.apply(p2.plan_id, { confirmed: true }), /component_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before); await notExists(f.setup.target);
});

test('replaced component directory cannot be removed even with identical files', async t => {
  const f = await fixture(t); const p = await f.preview(); await f.setup.apply(p.plan_id, { confirmed: true });
  await fs.rename(f.setup.target, f.setup.target + '.old');
  await fs.mkdir(f.setup.target, { mode: 0o700 });
  for (const name of files) await fs.copyFile(path.join(source, name), path.join(f.setup.target, name));
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /component_ownership_changed/);
  assert.equal(f.rows().find(x => x.id === ENTRY_ID).disabled, true);
});

test('removal failure after restoring patch stays unconfirmed and does not retry', async t => {
  let fault = false;
  const writer = { ...atomic, writeFileAtomic: async (...args) => {
    await atomic.writeFileAtomic(...args); if (fault) throw new Error('after removal write');
  } };
  const f = await fixture(t, { writer }); const p = await f.preview();
  await f.setup.apply(p.plan_id, { confirmed: true }); fault = true;
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /removal_incomplete/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.before);
  assert.equal((await f.setup.status(p.plan_id)).state, 'removal_unconfirmed');
  await assert.rejects(f.setup.remove(p.plan_id, { confirmed: true }), /incomplete_removal/);
});
