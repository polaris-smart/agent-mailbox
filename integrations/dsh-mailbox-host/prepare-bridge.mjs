// Private child protocol for the server-selected DSH SDK and launch context.
// Never boot a profile, evaluate expressions, activate a plugin or inherit secrets.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { planner } from './patch-plan.mjs';
import { Preparation } from './setup.mjs';

const LIMIT = 65536;
const own = fileURLToPath(import.meta.url);
const fail = () => { throw new Error('preparation_refused_or_incomplete'); };
const exact = (obj, names) => obj && typeof obj === 'object' && !Array.isArray(obj)
  && isDeepStrictEqual(Object.keys(obj).sort(), names.slice().sort());
const absolute = value => typeof value === 'string' && path.isAbsolute(value)
  && !/[\x00-\x1f\x7f]/.test(value);

async function privateDirectory(name) {
  if (!absolute(name) || await fs.realpath(name) !== name) fail();
  const s = await fs.lstat(name);
  if (!s.isDirectory() || s.uid !== process.getuid() || s.mode & 0o077) fail();
}

export async function makeAdapter(sdk, context, source = path.dirname(own)) {
  // The workbench must pin this SDK closure and resolve context itself before
  // invoking the child. These are internal inputs, not browser/mail fields.
  if (!absolute(sdk) || await fs.realpath(sdk) !== sdk
      || !exact(context, ['dir', 'home', 'installAnchor', 'patchPath', 'overlays', 'telemetryDisabledEnv'])
      || !absolute(context.installAnchor)
      || context.installAnchor !== path.join(sdk, '@deepseek-ai/dsh/package.json')
      || context.patchPath !== path.join(context.dir, 'cordis.patch.yml')
      || !Array.isArray(context.overlays)
      || !(context.telemetryDisabledEnv === null || typeof context.telemetryDisabledEnv === 'string')) fail();
  await privateDirectory(context.dir);
  await privateDirectory(context.home);
  // Detach caller-owned objects. Overlay changes require a new server snapshot.
  context = structuredClone(context);
  const load = name => import(pathToFileURL(path.join(sdk, name)).href);
  const yaml = (await load('js-yaml/index.js')).default;
  const include = await load('@deepseek-ai/cordis-plugin-include/lib/index.js');
  const cst = await load('yaml/dist/index.js');
  const boot = await load('@deepseek-ai/dsh-app-boot/lib/index.js');
  const atomic = await load('@deepseek-ai/dsh-atomic-write/lib/index.js');
  const prepare = planner({ yaml, ...include, cst });
  const readContext = async () => {
    // loadProfile() may initialize/rewrite a profile: deliberately never use it.
    const profile = boot.loadProfileDirectory('agent-mailbox', context.dir, context.installAnchor);
    if (profile.skippedBundles.length) fail();
    const beforePatches = profile.layers.flatMap(layer => layer.patches);
    const afterPatches = [
      ...boot.loadOptionalPatches('agent-mailbox', path.join(context.home, 'cordis.patch.yml')) ?? [],
      ...context.overlays,
    ];
    const layers = [beforePatches, profile.patches, afterPatches];
    const telemetry = boot.resolveTelemetryPatch(context.telemetryDisabledEnv,
      boot.composeEntries(layers).some(row => row.id === 'session-telemetry-otel'));
    if (telemetry !== undefined) afterPatches.push(telemetry);
    const all = [...beforePatches, ...profile.patches, ...afterPatches];
    const official = boot.readProfilePatches('agent-mailbox', context, profile);
    if (!isDeepStrictEqual(all, official)) fail();
    return { beforePatches, afterPatches };
  };
  return new Preparation({ profile: context.dir, source, prepare, readContext, atomic });
}

export async function execute(request) {
  const basic = ['version', 'operation', 'sdk', 'context'];
  const op = request?.operation;
  if (request?.version !== 1 || !['preview', 'apply', 'status', 'remove', 'reconcile'].includes(op)) fail();
  const fields = [...basic, ...(op === 'preview' ? ['options'] : ['plan_id']),
    ...(['apply', 'remove', 'reconcile'].includes(op) ? ['confirmed'] : [])];
  if (!exact(request, fields)) fail();
  if (op === 'preview' && !exact(request.options, ['home', 'socketPath'])) fail();
  if (op !== 'preview' && (typeof request.plan_id !== 'string'
      || !/^plan_[0-9a-f]{32}$/.test(request.plan_id))) fail();
  if (['apply', 'remove', 'reconcile'].includes(op) && request.confirmed !== true) fail();
  const adapter = await makeAdapter(request.sdk, request.context);
  const result = op === 'preview' ? await adapter.preview(request.options)
    : op === 'status' ? await adapter.status(request.plan_id)
      : await adapter[op](request.plan_id, { confirmed: true });
  // Only fixed status fields leave this process, never host config/paths/errors.
  return { plan_id: result.plan_id, state: result.state,
    requires_confirmation: op === 'preview', will_activate: false,
    session_verified: false, delivery_verified: false };
}

export async function main() {
  const output = process.stdout.write.bind(process.stdout);
  // Installed dependencies may log configuration on failure. Keep both streams
  // private until emitting our fixed projection. Parent also bounds output/time.
  const silent = (_chunk, encoding, callback) => {
    if (typeof encoding === 'function') encoding();
    else if (typeof callback === 'function') callback();
    return true;
  };
  process.stdout.write = silent; process.stderr.write = silent;
  try {
    let size = 0; const chunks = [];
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > LIMIT) fail();
      chunks.push(chunk);
    }
    const result = await execute(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    output(JSON.stringify({ version: 1, ok: true, result }) + '\n');
    return 0;
  } catch {
    output(JSON.stringify({ version: 1, ok: false, error: 'preparation_refused_or_incomplete' }) + '\n');
    return 2;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === own) process.exitCode = await main();
