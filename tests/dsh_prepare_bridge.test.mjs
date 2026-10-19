// Actual official SDK composition + isolated child protocol; no host boot/model.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { makeAdapter, execute } from '../integrations/dsh-mailbox-host/prepare-bridge.mjs';
import { ENTRY_ID } from '../integrations/dsh-mailbox-host/patch-plan.mjs';

const sdk = process.env.DSH_TEST_SDK;
if (!sdk || !path.isAbsolute(sdk)) throw new Error('explicit_verified_dsh_sdk_required');
const bridge = fileURLToPath(new URL('../integrations/dsh-mailbox-host/prepare-bridge.mjs', import.meta.url));
const boot = await import(pathToFileURL(path.join(sdk, '@deepseek-ai/dsh-app-boot/lib/index.js')).href);
const notExists = async p => assert.rejects(fs.lstat(p), { code: 'ENOENT' });

async function fixture(t) {
  const base = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-bridge-')));
  t.after(() => fs.rm(base, { recursive: true, force: true }));
  const profile = path.join(base, 'profile'); const home = path.join(base, 'dsh-home');
  const mailbox = path.join(base, 'mailbox');
  for (const p of [profile, home, mailbox]) await fs.mkdir(p, { mode: 0o700 });
  const manifest = path.join(profile, 'package.json');
  await fs.writeFile(manifest, JSON.stringify({ private: true,
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } } }), { mode: 0o600 });
  const patch = path.join(profile, 'cordis.patch.yml');
  const original = '# unrelated profile\n[]\n';
  await fs.writeFile(patch, original, { mode: 0o600 });
  const context = { dir: profile, home, installAnchor: path.join(sdk, '@deepseek-ai/dsh/package.json'),
    patchPath: patch, overlays: [], telemetryDisabledEnv: null };
  const request = { version: 1, operation: 'preview', sdk, context,
    options: { home: mailbox, socketPath: path.join(base, 'r.sock') } };
  const next = (operation, plan_id) => ({ version: 1, operation, sdk, context, plan_id,
    ...(['apply','remove','reconcile'].includes(operation) ? { confirmed: true } : {}) });
  const child = raw => spawnSync(process.execPath, [bridge], {
    input: typeof raw === 'string' ? raw : JSON.stringify(raw), encoding: 'utf8',
    cwd: base, timeout: 10000, maxBuffer: 65536,
    env: { PATH: '/usr/bin:/bin', HOME: mailbox, TMPDIR: base },
  });
  const rows = () => boot.composeEntries([boot.readProfilePatches('fixture', context)]);
  return { base, profile, home, mailbox, manifest, patch, original, context, request, next, child, rows };
}

test('real private child previews/applies/status/removes; SDK effective rows stay disabled', async t => {
  const f = await fixture(t);
  const first = f.child(f.request);
  assert.equal(first.status, 0, first.stderr); assert.equal(first.stderr, '');
  const preview = JSON.parse(first.stdout).result;
  assert.equal(preview.state, 'preview'); assert.equal(preview.requires_confirmation, true);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
  for (const [op, expected] of [['apply','prepared_disabled'], ['status','prepared_disabled'], ['remove','removed']]) {
    const p = f.child(f.next(op, preview.plan_id));
    assert.equal(p.status, 0, p.stderr + p.stdout); assert.equal(p.stderr, '');
    const result = JSON.parse(p.stdout).result;
    assert.equal(result.state, expected); assert.equal(result.session_verified, false);
    assert.equal(result.delivery_verified, false); assert.equal(result.will_activate, false);
    assert.ok(Object.keys(result).every(k => ['plan_id','state','requires_confirmation','will_activate',
      'session_verified','delivery_verified'].includes(k)));
    if (op !== 'remove') assert.equal(f.rows().find(r => r.id === ENTRY_ID).disabled, true);
  }
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
  await notExists(f.request.options.socketPath);
});

test('private child reconciles missing completion record only with explicit confirmation', async t => {
  const f = await fixture(t); const p = await execute(f.request);
  await execute(f.next('apply', p.plan_id));
  const record = path.join(f.profile, 'mailbox-install-plans', p.plan_id, 'prepared.json');
  await fs.unlink(record); // Crash boundary is injected in setup tests; exercise wire here.
  const unconfirmed = f.next('reconcile', p.plan_id); unconfirmed.confirmed = false;
  assert.equal(f.child(unconfirmed).status, 2); await notExists(record);
  const confirmed = f.child(f.next('reconcile', p.plan_id));
  assert.equal(confirmed.status, 0, confirmed.stdout + confirmed.stderr);
  assert.equal(confirmed.stderr, '');
  const result = JSON.parse(confirmed.stdout).result;
  assert.equal(result.state, 'prepared_disabled'); assert.equal(result.will_activate, false);
  assert.equal(result.session_verified, false); assert.equal(result.delivery_verified, false);
  await execute(f.next('remove', p.plan_id));
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
});

test('home overlay is included and receiver conflict refuses preparation', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.home, 'cordis.patch.yml'), `- id: ${ENTRY_ID}\n  disabled: false\n`, { mode: 0o600 });
  await assert.rejects(execute(f.request), /receiver_entry_conflict/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
  await notExists(path.join(f.profile, '.agent-mailbox-host'));
});

test('launcher overlay is not silently discarded', async t => {
  const f = await fixture(t);
  f.context.overlays = [{ id: ENTRY_ID, disabled: false }];
  await assert.rejects(execute(f.request), /receiver_entry_conflict/);
});

test('telemetry policy matches official complete layer composition', async t => {
  const f = await fixture(t); f.context.telemetryDisabledEnv = '0';
  // A custom profile supplies the telemetry row through a launcher overlay.
  // The standard dsh-base already has that id: inserting it twice tests an
  // ambiguous duplicate fixture, not the intended single-row telemetry policy.
  await fs.writeFile(f.manifest, JSON.stringify({ dsh: { profile: { bundles: [] } } }));
  f.context.overlays = [{ insert: [{ id: 'session-telemetry-otel', name: 'synthetic', disabled: false }] }];
  const adapter = await makeAdapter(sdk, f.context);
  const current = await adapter.inputs();
  assert.deepEqual(current.layers.afterPatches.at(-1), { id: 'session-telemetry-otel', disabled: true });
  const preview = await execute(f.request);
  await execute(f.next('apply', preview.plan_id));
  assert.equal(f.rows().find(r => r.id === 'session-telemetry-otel').disabled, true);
  assert.equal(f.rows().find(r => r.id === ENTRY_ID).disabled, true);
});

test('home overlay drift after preview refuses apply and preserves current bytes', async t => {
  const f = await fixture(t); const p = await execute(f.request);
  await fs.writeFile(path.join(f.home, 'cordis.patch.yml'), '- insert: [{id: later, name: synthetic}]\n', { mode: 0o600 });
  await assert.rejects(execute(f.next('apply', p.plan_id)), /configuration_changed/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
  await notExists(path.join(f.profile, '.agent-mailbox-host'));
});

test('skipped unsupported bundle is an error instead of an empty dependency layer', async t => {
  const f = await fixture(t);
  await fs.writeFile(f.manifest, JSON.stringify({ dsh: { profile: { bundles: ['@missing/nonexistent'] } } }));
  await assert.rejects(execute(f.request), /host_context_unreadable/);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
});

test('missing manifest is not initialized by the bridge', async t => {
  const f = await fixture(t); await fs.unlink(f.manifest);
  await assert.rejects(execute(f.request)); await notExists(f.manifest);
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
});

test('unrelated expression is parsed but never executed', async t => {
  const f = await fixture(t);
  await fs.writeFile(f.patch, '- insert:\n  - id: unrelated\n    name: synthetic\n    config:\n      trap: !!js "(() => {throw new Error(\'DO_NOT_EVALUATE\')})()"\n');
  const p = await execute(f.request); assert.equal(p.state, 'preview');
  await execute(f.next('apply', p.plan_id));
  assert.equal(f.rows().find(r => r.id === ENTRY_ID).disabled, true);
});

for (const [label, alter] of [
  ['missing overlays', r => { delete r.context.overlays; }],
  ['wrong patch', r => { r.context.patchPath = path.join(r.context.home, 'private'); }],
  ['foreign anchor', r => { r.context.installAnchor = '/private/tmp/foreign/package.json'; }],
  ['extra context', r => { r.context.shell = 'PRIVATE'; }],
  ['extra request', r => { r.shell = 'PRIVATE'; }],
  ['activation operation', r => { r.operation = 'enable'; }],
  ['extra options', r => { r.options.sessions = ['PRIVATE']; }],
]) test(`invalid ${label} refused without leaking request or paths`, async t => {
  const f = await fixture(t); alter(f.request);
  const p = f.child(f.request); assert.equal(p.status, 2); assert.equal(p.stderr, '');
  assert.deepEqual(JSON.parse(p.stdout), { version: 1, ok: false, error: 'preparation_refused_or_incomplete' });
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
  await notExists(path.join(f.profile, 'mailbox-install-plans'));
});

test('oversized input is refused with bounded redacted output', async t => {
  const f = await fixture(t); const p = f.child('PRIVATE'.repeat(11000));
  assert.equal(p.status, 2); assert.equal(p.stderr, ''); assert.ok(p.stdout.length < 200);
  await notExists(path.join(f.profile, 'mailbox-install-plans'));
});

test('apply requires literal confirmation and matching plan identity', async t => {
  const f = await fixture(t); const p = await execute(f.request);
  await assert.rejects(execute({ ...f.next('apply', p.plan_id), confirmed: 'true' }));
  await assert.rejects(execute({ ...f.next('apply', '../escape'), confirmed: true }));
  assert.equal(await fs.readFile(f.patch, 'utf8'), f.original);
});

test('caller overlay object is detached from a constructed adapter', async t => {
  const f = await fixture(t); const adapter = await makeAdapter(sdk, f.context);
  f.context.overlays.push({ id: ENTRY_ID, disabled: false });
  const p = await adapter.preview(f.request.options); assert.equal(p.state, 'preview');
  await assert.rejects(execute(f.request), /receiver_entry_conflict/);
});
