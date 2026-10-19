import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { apply, createReceiver, startReceiver } from './index.mjs';
import { deliver } from './client.mjs';

const binding = { id: 'binding_one', employee_id: 'employee_one', project_id: 'project_one', host_kind: 'deepseek', host_session_id: 'existing-session' };
const payload = () => ({ receipt_protocol: 'agent-mailbox.host-receipt.v1', delivery_id: 'wake-test', employee_id: binding.employee_id, binding: { ...binding }, fresh: ['message_one'] });
const retractPayload = () => ({ ...payload(), operation: 'retract_opened',
  receipt_protocol: 'agent-mailbox.host-retraction.v1' });
test('probe confirms the Host API without admitting mail or reading bindings', async () => {
  const receive = createReceiver({ loadBindings: async () => assert.fail('not required'), sessionController: { prompt: async () => assert.fail('not required') } });
  assert.deepEqual(await receive({ operation: 'probe' }), {
    protocol: 'agent-mailbox.host-receipt.v1', host_kind: 'deepseek',
    queue_supported: true, queue_retraction_supported: false,
  });
  const capable = createReceiver({ loadBindings: async () => assert.fail('not required'),
    sessionController: { prompt: async () => assert.fail('not required'),
      projections: async () => assert.fail('not required'),
      updateQueue: async () => assert.fail('not required') } });
  assert.equal((await capable({ operation: 'probe' })).queue_retraction_supported, true);
});
test('admission targets the existing session, queues, and keeps a stable request identity', async () => {
  const calls = [];
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: { prompt: async r => { calls.push(r); return { accepted: true }; } } });
  const [a, b] = await Promise.all([receive(payload()), receive(payload())]);
  assert.equal(a.status, 'received'); assert.deepEqual(a, b);
  assert.equal(calls[0].sessionId, binding.host_session_id); assert.equal(calls[0].mode, 'queue');
  assert.equal(calls[0].requestId, calls[1].requestId);
  assert.match(calls[0].content[0].text, /not a new Human instruction/);
});
test('discovery is workspace scoped, metadata only, and never activates a session', async () => {
  let reads = 0;
  const receive = createReceiver({ loadBindings: async () => assert.fail('not required'), sessionController: {
    prompt: async () => assert.fail('must not activate'),
    list: async () => { reads++; return { items: [
      { sessionId: 'one', cwd: '/workspace', agentAvailable: true, running: false,
        projections: { private: 'omit' }, provider: 'omit' },
      { sessionId: 'other', cwd: '/other', agentAvailable: true },
      { sessionId: 'child', cwd: '/workspace', parentSessionId: 'one' },
      { sessionId: 'blank', cwd: '/workspace', blank: true },
    ] }; },
  } });
  await assert.rejects(receive({ operation: 'list_sessions', cwd: 'relative' }), /workspace_required/);
  assert.equal(reads, 0);
  assert.deepEqual((await receive({ operation: 'list_sessions', cwd: '/workspace' })).sessions,
    [{ session_id: 'one', cwd: '/workspace', running: false, agent_available: true }]);
  assert.deepEqual((await receive({ operation: 'list_sessions', cwd: '/other' })).sessions,
    [{ session_id: 'other', cwd: '/other', running: null, agent_available: true }]);
});
test('whole-profile activity gate fails closed on running, unknown, or empty lists', async () => {
  let items = [
    { sessionId: 'bound', cwd: '/workspace', running: false },
    { sessionId: 'other-workspace', cwd: '/other', running: true },
  ];
  const receive = createReceiver({ loadBindings: async () => assert.fail('not required'),
    sessionController: {
      prompt: async () => assert.fail('must not prompt'),
      list: async () => ({ items }),
    } });
  assert.deepEqual(await receive({ operation: 'session_activity' }), {
    protocol: 'agent-mailbox.host-receipt.v1', host_kind: 'deepseek',
    total: 2, running: 1, unknown: 0, idle_confirmed: false,
  });
  items = [{ sessionId: 'bound', cwd: '/workspace', running: false },
    { sessionId: 'unknown', cwd: '/other' }];
  assert.equal((await receive({ operation: 'session_activity' })).idle_confirmed, false);
  assert.equal((await receive({ operation: 'session_activity' })).unknown, 1);
  items = [{ sessionId: 'bound', cwd: '/workspace', running: false }];
  assert.equal((await receive({ operation: 'session_activity' })).idle_confirmed, true);
  items = [];
  assert.equal((await receive({ operation: 'session_activity' })).idle_confirmed, false);
});
test('revoked, ambiguous, and changed bindings do not call the host', async () => {
  for (const bindings of [[], [binding, binding], [{ ...binding, id: 'binding_new' }]]) {
    const receive = createReceiver({ loadBindings: async () => bindings, sessionController: { prompt: async () => assert.fail('must not deliver') } });
    assert.equal((await receive(payload())).status, 'not_received');
  }
});
test('malformed and cross-employee input is rejected before admission', async () => {
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: { prompt: async () => assert.fail('must not deliver') } });
  for (const input of [null, { ...payload(), employee_id: 'employee_other' }, { ...payload(), fresh: ['../../x'] }, { ...payload(), fresh: ['message_one', 'message_one'] }]) {
    await assert.rejects(receive(input), /invalid_delivery/);
  }
});
test('a host exception or non-acceptance is unknown, not a negative receipt', async () => {
  for (const prompt of [async () => { throw new Error('after admission'); }, async () => ({ accepted: false })]) {
    const receive = createReceiver({ loadBindings: async () => [binding], sessionController: { prompt } });
    await assert.rejects(receive(payload()));
  }
});
test('opened-mail retraction removes only its exact pending DSH queue item', async () => {
  const calls = [], pending = [];
  const controller = {
    async prompt(request) {
      pending.push({ id: 'item_target', source: { kind: 'user', rpcId: request.requestId } });
      return { accepted: true };
    },
    async projections(request) {
      calls.push(['projection', request.sessionId]);
      return { values: { inbox: {
        'next-turn': [
          { id: 'item_other', source: { kind: 'user', rpcId: 'another-request' } },
          ...pending,
        ],
        'next-step': [],
      } } };
    },
    async updateQueue(request) { calls.push(['remove', request]); return { accepted: true }; },
    cancel() { assert.fail('retraction must not cancel a running Agent'); },
  };
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: controller });
  assert.equal((await receive(payload())).status, 'received');
  const result = await receive(retractPayload());
  assert.equal(result.protocol, 'agent-mailbox.host-retraction.v1');
  assert.equal(result.status, 'retracted');
  assert.deepEqual(calls, [
    ['projection', binding.host_session_id],
    ['remove', { sessionId: binding.host_session_id,
      itemId: 'item_target', action: { kind: 'remove' } }],
  ]);
});
test('stale queue observation cannot cancel a turn already claimed by DSH', async () => {
  let requestId, removed = 0;
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: {
    async prompt(request) { requestId = request.requestId; return { accepted: true }; },
    async projections() { return { values: { inbox: {
      'next-turn': [{ id: 'item_target', source: { kind: 'user', rpcId: requestId } }],
      'next-step': [],
    } } }; },
    async updateQueue() { removed++; throw Object.assign(new Error('claimed'), { code: 'session/queue-item-not-found' }); },
    cancel() { assert.fail('live Agent turn must continue'); },
  } });
  await receive(payload());
  assert.equal((await receive(retractPayload())).status, 'not_pending');
  assert.equal(removed, 1);
});
test('queue retraction fails closed on changed binding, ambiguity, and missing projection', async () => {
  const controller = { prompt: async () => assert.fail('must not prompt'),
    projections: async () => assert.fail('must not inspect'),
    updateQueue: async () => assert.fail('must not remove'),
  };
  const changed = createReceiver({ loadBindings: async () => [], sessionController: controller });
  assert.equal((await changed(retractPayload())).status, 'binding_changed');
  let target;
  const ambiguous = createReceiver({ loadBindings: async () => [binding], sessionController: {
    async prompt(request) { target = request.requestId; return { accepted: true }; },
    async projections() { return { values: { inbox: {
      'next-turn': [{ id: 'a', source: { kind: 'user', rpcId: target } }],
      'next-step': [{ id: 'b', source: { kind: 'user', rpcId: target } }],
    } } }; },
    updateQueue: controller.updateQueue,
  } });
  await ambiguous(payload());
  await assert.rejects(ambiguous(retractPayload()), /ambiguous_host_queue_item/);
  const unavailable = createReceiver({ loadBindings: async () => [binding], sessionController: {
    projections: async () => ({ values: {} }), updateQueue: controller.updateQueue,
  } });
  await assert.rejects(unavailable(retractPayload()), /host_inbox_projection_unavailable/);
  await assert.rejects(unavailable({ ...payload(), operation: 'retract_opened' }), /invalid_delivery/);
});

async function fixture(t) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), 'am-host-'));
  t.after(() => fs.rm(home, { recursive: true, force: true }));
  await fs.mkdir(path.join(home, 'wake', 'wake-outbox'), { recursive: true });
  await fs.writeFile(path.join(home, 'wake', 'wake-outbox', 'wake-test.json'), JSON.stringify(payload()));
  return { home, socketPath: path.join(home, 's'), employeeId: binding.employee_id, deliveryId: 'wake-test' };
}
test('plugin owns the async listener as a lifecycle effect and releases it on unload', async t => {
  const args = await fixture(t), effects = [];
  const ctx = { sessionController: {},
    on() {}, // Event listeners are not plugin-unload callbacks in Cordis.
    effect(start) { const setup = Promise.resolve(start()); effects.push(setup); return setup; },
  };
  await apply(ctx, args);
  assert.equal(effects.length, 1);
  for (const setup of effects) await (await setup)();
  await assert.rejects(fs.stat(args.socketPath), { code: 'ENOENT' });
  const close = await startReceiver({ ...args, receive: async () => ({}) });
  await close();
});
test('real Unix socket and client persist only the matched Host receipt', async t => {
  const args = await fixture(t), seen = new Set();
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: { prompt: async r => { seen.add(r.requestId); return { accepted: true }; } } });
  const close = await startReceiver({ ...args, receive }); t.after(close);
  const r = await deliver(args); assert.equal(r.status, 'received');
  await deliver(args); assert.equal(seen.size, 1);
  const file = path.join(args.home, 'wake', 'receipts', args.deliveryId);
  assert.deepEqual(JSON.parse(await fs.readFile(file)), r);
  assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
});
test('Unix socket retracts one queued reminder and rejects the old wire protocol', async t => {
  const args = await fixture(t), queue = [], removed = [];
  let prompts = 0;
  const receive = createReceiver({ loadBindings: async () => [binding], sessionController: {
    async prompt(request) {
      prompts++;
      queue.push({ id: 'item_one', source: { kind: 'user', rpcId: request.requestId } });
      return { accepted: true };
    },
    async projections() { return { values: { inbox: { 'next-turn': queue, 'next-step': [] } } }; },
    async updateQueue(request) {
      removed.push(request);
      queue.splice(queue.findIndex(item => item.id === request.itemId), 1);
      return { accepted: true };
    },
    cancel() { assert.fail('must not cancel active work'); },
  } });
  const close = await startReceiver({ ...args, receive }); t.after(close);
  const ask = request => new Promise((resolve, reject) => {
    const socket = net.createConnection(args.socketPath);
    let raw = '';
    socket.on('connect', () => socket.write(JSON.stringify(request) + '\n'));
    socket.on('data', chunk => { raw += chunk; });
    socket.on('end', () => {
      try { resolve(JSON.parse(raw.trim())); } catch (error) { reject(error); }
    });
    socket.on('error', reject);
  });
  assert.equal((await ask(payload())).status, 'received');
  assert.deepEqual(await ask({ ...payload(), operation: 'retract_opened' }),
    { error: 'delivery_unconfirmed' });
  assert.equal(queue.length, 1);
  assert.equal((await ask(retractPayload())).status, 'retracted');
  assert.equal(queue.length, 0);
  assert.equal((await ask(retractPayload())).status, 'not_pending');
  assert.equal(prompts, 1);
  assert.deepEqual(removed, [{ sessionId: binding.host_session_id,
    itemId: 'item_one', action: { kind: 'remove' } }]);
});
test('offline socket creates a retryable negative receipt without starting a host', async t => {
  const args = await fixture(t);
  assert.equal((await deliver(args)).status, 'not_received');
});
test('lost response after connect does not fabricate any receipt', async t => {
  const args = await fixture(t);
  const server = net.createServer(s => s.on('data', () => s.end()));
  await new Promise(r => server.listen(args.socketPath, r));
  t.after(() => new Promise(r => server.close(r)));
  await assert.rejects(deliver(args), /unknown/);
  await assert.rejects(fs.stat(path.join(args.home, 'wake', 'receipts', args.deliveryId)), { code: 'ENOENT' });
});
test('never removes an existing endpoint or opens a shared directory', async t => {
  const args = await fixture(t); await fs.writeFile(args.socketPath, 'preserve');
  await assert.rejects(startReceiver({ ...args, receive: async () => ({}) }), /already_exists/);
  assert.equal(await fs.readFile(args.socketPath, 'utf8'), 'preserve');
  await fs.unlink(args.socketPath); await fs.chmod(args.home, 0o755);
  await assert.rejects(startReceiver({ ...args, receive: async () => ({}) }), /owner_private/);
});
