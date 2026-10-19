// Exercise the real CLI setup event flow without touching a mailbox or host.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const start = source.indexOf('function openCodexCliReceiverSettings(');
const end = source.indexOf('\nfunction openReceiverSettings(', start);
assert(start >= 0 && end > start);

const elements = [];
const buttons = [];
const make = (tag, attributes = {}, children = []) => {
  const node = {
    tag, id: attributes.id, value: attributes.value || '', checked: Boolean(attributes.checked),
    disabled: false, isConnected: true, events: {},
    append() {}, replaceChildren() {},
    addEventListener(type, handler) { this.events[type] = handler; },
  };
  if (tag === 'select' && children.length) {
    const first = Array.isArray(children[0]) ? children[0][0] : children[0];
    node.value = first?.value || '';
  }
  elements.push(node);
  return node;
};
const calls = [];
let bindingId = null;
let installed = false;
const employee = {id: 'employee-cli', kind: 'codex', connection_type: 'cli', project_ids: ['project-1']};
const scope = {
  state: {projectId: 'project-1', data: {projects: [{id: 'project-1', name: 'Synthetic project'}]}},
  formDialog: {open: true},
  openForm: () => make('body'),
  formField: (_name, node) => node,
  errorBox: () => make('error'),
  formError: () => assert.fail('No setup action should fail'),
  t: value => value,
  el: (tag, attributes, ...children) => make(tag, attributes, children),
  button: label => {
    const control = make('button');
    control.label = label;
    buttons.push(control);
    return control;
  },
  api: {
    receiverStatus: async () => ({state: 'binding_required', installed, binding_id: bindingId}),
    receiverAction: async request => {
      calls.push(request);
      if (request.action === 'install') {
        installed = true;
        return {state: 'binding_required', installed: true, binding_id: null};
      }
      if (request.action === 'cli_preflight') {
        return {thread_id: request.thread_id, workspace_verified: true};
      }
      if (request.action === 'cli_bind') {
        bindingId = 'binding-cli';
        return {state: 'cli_install_required', binding_id: bindingId};
      }
      if (request.action === 'cli_enable') {
        return {state: 'cli_launch_requested', binding_id: bindingId, receiver_online: false};
      }
      assert.fail(`Unexpected action ${request.action}`);
    },
  },
};
const open = vm.runInNewContext(`(${source.slice(start, end)})`, scope);
const click = async label => {
  const control = buttons.find(item => item.label === label);
  assert(control, `Missing ${label}`);
  assert.equal(control.disabled, false, `${label} must be enabled`);
  await control.events.click();
};

(async () => {
  open(employee);
  await new Promise(setImmediate);
  const thread = elements.find(item => item.id === 'cli-receiver-thread');
  const consent = elements.find(item => item.id === 'cli-receiver-consent');
  assert(thread && consent);
  await click('安装接收组件');
  thread.value = '12345678-1234-1234-1234-123456789abc';
  thread.events.input();
  await click('核对已有会话');
  consent.checked = true;
  consent.events.change();
  await click('连接固定信箱');
  await click('启用按需接收');
  assert.deepEqual(calls.map(call => call.action), ['install', 'cli_preflight', 'cli_bind', 'cli_enable']);
  assert.equal(calls[2].expected_binding_id, null);
  assert.equal(calls[3].expected_binding_id, 'binding-cli');
  assert.equal(calls[3].thread_id, thread.value);
  assert.equal(calls[3].confirmed, true);
  const guiStart = source.indexOf('function openReceiverSettings(', end);
  const guiEnd = source.indexOf('\nfunction renderMailboxes(', guiStart);
  assert(guiStart >= 0 && guiEnd > guiStart);
  let cliRouted = 0;
  const dispatch = vm.runInNewContext(`(${source.slice(guiStart, guiEnd)})`, {
    openCodexCliReceiverSettings: person => { assert.equal(person.id, employee.id); cliRouted++; },
    openForm: () => assert.fail('CLI employee must never enter desktop App setup'),
  });
  dispatch(employee);
  assert.equal(cliRouted, 1);
  console.log('CLI fixed mailbox setup event flow: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
