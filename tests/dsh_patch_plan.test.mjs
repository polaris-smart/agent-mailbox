// Requires the verified official SDK closure from DSH; no downloads or host startup.
import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';
import os from 'node:os';
import { planner, ENTRY_ID, MODULE_NAME } from '../integrations/dsh-mailbox-host/patch-plan.mjs';

const root = process.env.DSH_TEST_SDK;
if (!root || !path.isAbsolute(root)) throw new Error('explicit_verified_dsh_sdk_required');
const load = relative => import(pathToFileURL(path.join(root, relative)).href);
const yaml = (await load('js-yaml/index.js')).default;
const { entryListSchema, applyEntryPatches } = await load('@deepseek-ai/cordis-plugin-include/lib/index.js');
const cst = await load('yaml/dist/index.js');
const prepare = planner({ yaml, entryListSchema, applyEntryPatches, cst });
const base = [{ insert: [{ id: 'existing', name: 'original', config: { enabled: true } }] }];
const options = () => ({ home: '/private/tmp/synthetic-home', socketPath: '/private/tmp/synthetic.sock',
                        profileDir: '/private/tmp/profile',
                        beforePatches: structuredClone(base), afterPatches: [] });
const parse = text => yaml.load(text, { schema: entryListSchema });

test('old bare-entry patch does not install: official composer regression oracle', () => {
  const warnings = [];
  const result = applyEntryPatches([], parse('- id: dsh-mailbox-host\n  name: agent-mailbox-dsh-host\n'),
                                   message => warnings.push(message));
  assert.equal(result.length, 0);
  assert.equal(warnings.length, 1);
});

test('new patch actually inserts exactly one disabled receiver', () => {
  const result = prepare('# keep this comment\n- id: existing\n  config: {enabled: false}\n', options());
  assert.match(result.text, /keep this comment/);
  const rows = applyEntryPatches([], [...base, ...parse(result.text)], assert.fail);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].config, { enabled: false });
  assert.deepEqual(rows[1], result.entry);
  assert.equal(rows[1].disabled, true);
  assert.equal(result.activationVerified, false);
  assert.equal(result.sessionVerified, false);
});

test('official mutating composer cannot pollute caller layers or change repeated plans', () => {
  const o = options();
  o.afterPatches = [
    { insert: [{ id: 'telemetry', name: 'synthetic', disabled: false, config: { enabled: true } }] },
    { id: 'telemetry', disabled: true, config: { enabled: false } },
  ];
  const original = structuredClone(o);
  const first = prepare('[]', o);
  assert.deepEqual(o, original);
  const second = prepare('[]', o);
  assert.deepEqual(o, original);
  assert.equal(second.text, first.text);
  assert.equal(second.afterSha256, first.afterSha256);
});

test('quoted unicode escape cannot hide same identity', () => {
  assert.throws(() => prepare('- insert: [{id: "dsh-mailbox-ho\\u0073t", name: unrelated}]\n', options()),
                /receiver_entry_conflict/);
});

test('identity aliases and flow-style lists are parsed by real schema', () => {
  assert.throws(() => prepare('- insert:\n  - id: &x dsh-mailbox-host\n    name: first\n  - id: *x\n    name: second\n', options()),
                /receiver_entry_conflict/);
  const result = prepare('[{id: existing, config: {enabled: false}}]\n', options());
  assert.equal(parse(result.text).length, 2);
});

test('nested group receiver conflict cannot hide in config', () => {
  const o = options();
  o.beforePatches = [{ insert: [{ id: 'group', group: true, config: [{ id: ENTRY_ID, name: MODULE_NAME }] }] }];
  assert.throws(() => prepare('[]', o), /receiver_entry_conflict/);
});

test('later overriding layer cannot steal receiver identity', () => {
  const o = options();
  o.afterPatches = [{ id: ENTRY_ID, disabled: false }];
  assert.throws(() => prepare('[]', o), /receiver_entry_conflict/);
});

test('existing equivalent module with different id conflicts', () => {
  assert.throws(() => prepare(`- insert: [{id: alternate, name: '${MODULE_NAME}'}]\n`, options()),
                /receiver_entry_conflict/);
});

test('normalized file URL and relative aliases cannot hide the owned module', () => {
  for (const name of ['file:///private/tmp/profile/.agent-mailbox-host/index.mjs',
                      './other/../.agent-mailbox-host/index.mjs',
                      '/private/tmp/profile/.agent-mailbox-host/index.mjs']) {
    const o = options();
    o.beforePatches = [{ insert: [{ id: 'alternate', name }] }];
    assert.throws(() => prepare('[]', o), /receiver_entry_conflict/);
  }
});

test('profile directory must be explicit and absolute', () => {
  for (const profileDir of [undefined, 'relative', '/private/tmp/x\n']) {
    assert.throws(() => prepare('[]', { ...options(), profileDir }), /invalid_private_path/);
  }
});

test('dynamic identity refused rather than evaluated', () => {
  assert.throws(() => prepare('- insert: [{id: !!js "ctx.chooseId()", name: anything}]\n', options()),
                /dynamic_plugin_identity_refused/);
});

test('unrelated expressions stay inert with official semantic equality', () => {
  const text = '- id: existing\n  config:\n    flag: !!js "(() => { throw new Error(\'DO_NOT_RUN\') })()"\n';
  const result = prepare(text, options());
  assert.deepEqual(parse(result.text)[0], parse(text)[0]);
  assert.equal(parse(result.text)[0].config.flag.__jsExpr.includes('DO_NOT_RUN'), true);
});

test('literal blocks and comments preserved', () => {
  const text = '# preference\n- id: existing # selected row\n  config:\n    note: |\n      first line\n      second line\n';
  const result = prepare(text, options());
  assert.match(result.text, /# preference/);
  assert.match(result.text, /# selected row/);
  assert.deepEqual(parse(result.text)[0], parse(text)[0]);
});

test('empty document and empty flow sequence both work', () => {
  for (const text of ['', '# empty\n', '[]\n']) assert.equal(parse(prepare(text, options()).text).length, 1);
});

test('malformed and duplicate-key YAML never silently repaired', () => {
  for (const text of ['- [broken', '- id: a\n  id: b\n', 'key: value\n']) {
    assert.throws(() => prepare(text, options()));
  }
});

test('missing actual runtime layers is not treated as empty', () => {
  const o = options(); delete o.afterPatches;
  assert.throws(() => prepare('[]', o), /explicit_profile_layers_required/);
});

test('unrelated warning in effective composition blocks preparation', () => {
  assert.throws(() => prepare('- id: missing\n  config: {}\n', options()), /existing_patch_warnings/);
});

test('scalar injection and relative paths refused', () => {
  for (const value of ['/private/tmp/x\n- insert: []', '/private/tmp/x\0', 'relative']) {
    assert.throws(() => prepare('[]', { ...options(), home: value }), /invalid_private_path/);
  }
});

test('quotes and unicode in paths remain scalar values', () => {
  const home = "/private/tmp/合成 ' # : home";
  assert.equal(prepare('[]', { ...options(), home }).entry.config.home, home);
});

test('overlong source and unix socket path refused', () => {
  assert.throws(() => prepare(' '.repeat(1024 * 1024 + 1), options()), /patch_size_limit/);
  assert.throws(() => prepare('[]', { ...options(), socketPath: '/' + 's'.repeat(101) }), /socket_path_too_long/);
});

test('input patch objects never mutated', () => {
  const o = options(); const snapshot = structuredClone(o);
  prepare('[]', o);
  assert.deepEqual(o, snapshot);
});

test('recursive insertion graph refused before composition', () => {
  const o = options(); const recursive = []; recursive.push({ insert: recursive });
  o.beforePatches = recursive;
  assert.throws(() => prepare('[]', o), /recursive_patch_entries/);
});

test('real profile loader composes prepared row after shipped bundle', async () => {
  const boot = await load('@deepseek-ai/dsh-app-boot/lib/index.js');
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-patch-profile-'));
  try {
    await fs.writeFile(path.join(profile, 'package.json'), JSON.stringify({
      private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'] } },
    }));
    await fs.writeFile(path.join(profile, 'cordis.patch.yml'), '# retained user comment\n[]\n');
    const anchor = path.join(root, '@deepseek-ai/dsh/package.json');
    const before = boot.loadProfileDirectory('mailbox-test', profile, anchor);
    assert.deepEqual(before.skippedBundles, []);
    assert.equal(before.layers.length, 1);
    const beforePatches = before.layers.flatMap(layer => layer.patches);
    const prepared = prepare('# retained user comment\n[]\n', {
      ...options(), profileDir: profile, beforePatches, afterPatches: [],
    });
    await fs.writeFile(path.join(profile, 'cordis.patch.yml'), prepared.text);
    const after = boot.loadProfileDirectory('mailbox-test', profile, anchor);
    const rows = boot.composeEntries([...after.layers.map(layer => layer.patches), after.patches], assert.fail);
    const found = rows.filter(row => row.id === ENTRY_ID);
    assert.deepEqual(found, [{ ...prepared.entry,
      name: pathToFileURL(path.join(profile, MODULE_NAME)).href }]);
    assert.equal(found[0].disabled, true);
    assert.deepEqual(rows.filter(row => row.id !== ENTRY_ID), boot.composeEntries([beforePatches], assert.fail));
  } finally { await fs.rm(profile, { recursive: true, force: true }); }
});
