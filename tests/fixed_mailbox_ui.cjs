const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const start = source.indexOf('function renderMailboxes()');
const end = source.indexOf('\nfunction renderMembers()', start);
assert(start >= 0 && end > start);

const el = (tag, props = {}, ...children) => ({
  tag, props, children: children.flat(Infinity).filter(child => child != null), events: {},
  addEventListener(name, handler) { this.events[name] = handler; },
  append(...next) { this.children.push(...next); },
});
const cli = {id: 'cli', name: 'Codex CLI', kind: 'codex', connection_type: 'cli',
  project_ids: ['p1', 'p2'], node_id: 'local'};
const app = {id: 'app', name: 'Codex App', kind: 'codex', connection_type: 'app',
  project_ids: ['p1'], node_id: 'local'};
const state = {
  mailboxEmployeeId: 'cli', mailboxFolder: 'inbox', mailboxMessageId: '', messageDrafts: {},
  data: {
    node: {id: 'local', name: 'Local Mac'}, devices: [{id: 'local', name: 'Local Mac'}], employees: [cli, app],
    projects: [{id: 'p1', name: 'Project A'}, {id: 'p2', name: 'Project B'}],
    messages: [
      {id: 'm1', project_id: 'p1', recipient_id: 'cli', sender_id: 'human', title: 'First', body: 'A', created_at: '2026-01-01T00:00:00Z'},
      {id: 'm2', project_id: 'p2', recipient_id: 'cli', sender_id: 'human', title: 'Second', body: 'B', created_at: '2026-01-02T00:00:00Z'},
      {id: 'm3', project_id: 'p1', recipient_id: 'app', sender_id: 'human', title: 'App only', body: 'C', created_at: '2026-01-03T00:00:00Z'},
      {id: 'm4', project_id: 'p2', recipient_id: null, sender_id: 'human', title: 'Project B broadcast', body: 'D', created_at: '2026-01-04T00:00:00Z'},
    ],
  },
};
let renders = 0;
const context = {
  state, el, t: first => first, icon: () => '', avatar: () => '',
  button: (label) => el('button', {}, label), heading: (...args) => el('header', {}, ...args),
  empty: () => el('div'), navLabels: {mailboxes: '员工信箱'},
  formatDate: value => value, render: () => { renders++; },
  selectProject: () => {}, selectView: () => {}, loadMessages: () => {},
  openReceiverSettings: () => {}, openNativeReceiverStatus: () => {},
};
vm.runInNewContext(source.slice(source.indexOf('function project()'), source.indexOf('function formatDate(')), context);
const renderMailboxes = vm.runInNewContext(`(${source.slice(start, end)})`, context);
const sections = renderMailboxes();
const [rail, listPane] = sections[1].children;
assert.equal(rail.children.length, 2, 'one rail entry per agent instance, not per project');
assert.equal(listPane.children[1].children.length, 2, 'one CLI mailbox contains mail from both projects');
assert.deepEqual(listPane.children[1].children.map(row => row.children[2].children[0]),
  ['Project B', 'Project A'], 'each letter retains its source project');
assert.equal(state.mailboxMessageId, 'm2');

rail.children[1].events.click();
assert.equal(state.mailboxEmployeeId, 'app', 'desktop app remains a separate instance');
assert.equal(renders, 1);
const appList = renderMailboxes()[1].children[1].children[1];
assert.equal(appList.children.length, 1, 'App mailbox does not inherit CLI mail');
assert.equal(appList.children[0].props['data-message-id'], 'm3');

state.mailboxFolder = 'group';
const appBroadcast = renderMailboxes()[1].children[1].children[1];
assert.equal(appBroadcast.children.length, 1);
assert.equal(appBroadcast.children[0].props.class, 'mailbox-empty',
  'App has no access to a broadcast in a project it has not joined');
state.mailboxEmployeeId = 'cli';
const cliBroadcast = renderMailboxes()[1].children[1].children[1];
assert.equal(cliBroadcast.children.length, 1);
assert.equal(cliBroadcast.children[0].props['data-message-id'], 'm4');
console.log('fixed mailbox multi-project UI: PASS');
