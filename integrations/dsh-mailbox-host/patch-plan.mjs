// Pure preparation plan; no filesystem writes, plugin activation or expression evaluation.
// Caller supplies the verified host SDK and the actual layers before/after this profile.
import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ENTRY_ID = 'dsh-mailbox-host';
export const MODULE_NAME = './.agent-mailbox-host/index.mjs';
const MAX_BYTES = 1024 * 1024;
const digest = text => createHash('sha256').update(text).digest('hex');
const record = x => x !== null && typeof x === 'object' && !Array.isArray(x);

function boundedEntries(entries, visit, budget = { left: 20000, seen: new WeakSet() }) {
  if (!Array.isArray(entries) || budget.left-- <= 0 || budget.seen.has(entries)) {
    throw new Error('invalid_or_recursive_patch_entries');
  }
  budget.seen.add(entries);
  for (const entry of entries) {
    if (!record(entry) || budget.left-- <= 0) throw new Error('invalid_patch_entry');
    for (const field of ['id', 'name']) {
      if (entry[field] !== undefined && typeof entry[field] !== 'string') {
        throw new Error('dynamic_plugin_identity_refused');
      }
    }
    visit(entry);
    if (entry.insert !== undefined) boundedEntries(entry.insert, visit, budget);
    if (entry.group && Array.isArray(entry.config)) boundedEntries(entry.config, visit, budget);
  }
}

export function planner({ yaml, entryListSchema, applyEntryPatches, cst }) {
  if (!yaml?.load || !entryListSchema || !applyEntryPatches || !cst?.parseDocument) {
    throw new Error('official_host_yaml_sdk_required');
  }
  const parse = text => {
    if (typeof text !== 'string' || Buffer.byteLength(text) > MAX_BYTES) {
      throw new Error('patch_size_limit');
    }
    const result = yaml.load(text, { schema: entryListSchema });
    if (result === undefined || result === null) return [];
    if (!Array.isArray(result)) throw new Error('patch_list_required');
    return result;
  };
  const compose = patches => {
    const warnings = [];
    // Official composition mutates inserted rows when later patches override them.
    // This planner is pure: never let that mutate the caller's launch snapshot.
    const entries = applyEntryPatches([], structuredClone(patches), message => warnings.push(message));
    if (warnings.length) throw new Error('existing_patch_warnings_require_resolution');
    return entries;
  };

  return function prepare(text, { home, socketPath, profileDir, beforePatches, afterPatches } = {}) {
    // Missing runtime composition context is not equivalent to empty layers.
    if (!Array.isArray(beforePatches) || !Array.isArray(afterPatches)) {
      throw new Error('explicit_profile_layers_required');
    }
    for (const value of [home, socketPath, profileDir]) {
      if (typeof value !== 'string' || !path.isAbsolute(value) || value.length > 4096
          || /[\u0000-\u001f\u007f]/u.test(value)) throw new Error('invalid_private_path');
    }
    if (Buffer.byteLength(socketPath) > 100) throw new Error('socket_path_too_long');
    // This is lexical normalization only. The writing bridge must separately verify
    // canonical, privately owned paths; pure planning must not follow disk links.
    const destination = path.resolve(profileDir, MODULE_NAME);
    const isOurModule = name => {
      if (typeof name !== 'string') return false;
      if (name.startsWith('file:')) {
        try { return path.resolve(fileURLToPath(name)) === destination; }
        catch { throw new Error('invalid_plugin_file_url'); }
      }
      return (name.startsWith('.') || path.isAbsolute(name))
        && path.resolve(profileDir, name) === destination;
    };
    const original = parse(text);
    const layers = [...beforePatches, ...original, ...afterPatches];
    boundedEntries(layers, entry => {
      if (entry.id === ENTRY_ID || isOurModule(entry.name)
          || entry.name === 'agent-mailbox-dsh-host'
          || entry.name === './node_modules/agent-mailbox-dsh-host/index.mjs') {
        throw new Error('receiver_entry_conflict');
      }
    });
    const before = compose(layers);
    const entry = {
      id: ENTRY_ID, name: MODULE_NAME, disabled: true,
      config: { home, socketPath },
    };
    // Parse for comment/style preservation; official schema above/below is the semantic oracle.
    // !!js is retained as an inert tagged string. Neither parser evaluates it.
    const doc = cst.parseDocument(text, { customTags: [{
      tag: 'tag:yaml.org,2002:js', resolve: value => value,
      stringify: item => JSON.stringify(item.value),
    }] });
    if (doc.errors.length || doc.warnings.length) throw new Error('cst_parse_failed');
    if (doc.contents === null) doc.contents = doc.createNode([]);
    if (!cst.isSeq(doc.contents)) throw new Error('patch_list_required');
    doc.add({ insert: [entry] });
    const candidate = doc.toString();
    const decoded = parse(candidate);
    if (decoded.length !== original.length + 1
        || !isDeepStrictEqual(decoded.slice(0, -1), original)
        || !isDeepStrictEqual(decoded.at(-1), { insert: [entry] })) {
      throw new Error('unrelated_patch_semantics_changed');
    }
    const after = compose([...beforePatches, ...decoded, ...afterPatches]);
    const ours = after.filter(row => row.id === ENTRY_ID);
    if (ours.length !== 1 || !isDeepStrictEqual(ours[0], entry)
        || !isDeepStrictEqual(after.filter(row => row.id !== ENTRY_ID), before)) {
      throw new Error('effective_patch_scope_changed');
    }
    return {
      beforeSha256: digest(text), afterSha256: digest(candidate), text: candidate,
      entry, preparedOnly: true, activationVerified: false, sessionVerified: false,
    };
  };
}
