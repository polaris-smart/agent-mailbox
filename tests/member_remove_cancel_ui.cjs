// Exercise the real employee-row click handler without starting a GUI browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const start = source.indexOf('function employeeRow(');
const end = source.indexOf('\n// 四动词', start);
assert(start >= 0 && end > start, 'employeeRow must be present');

const buttons = [];
const node = () => ({
  disabled: false,
  events: {},
  classList: {add() {}},
  append() {},
  replaceChildren() {},
  addEventListener(name, handler) { this.events[name] = handler; },
});
const employee = {
  id: 'employee-1', name: 'Test employee', kind: 'codex', node_id: 'node-1',
  lifecycle: 'active', auth_status: 'unknown', project_ids: ['project-1'],
  execution_supported: false,
};
let confirmRemoval = false;
let removeCalls = 0;
const scope = {
  state: {projectId: 'project-1', data: {projects: [{id: 'project-1', name: 'Test project'}], employees: [employee]}},
  window: {confirm: () => confirmRemoval},
  api: {removeProjectMember: async () => { removeCalls++; }},
  refresh: async () => {},
  showToast: () => {},
  t: value => value,
  el: () => node(),
  button(label, _action, options = {}) {
    const control = node();
    control.label = label;
    control.disabled = Boolean(options.disabled);
    buttons.push(control);
    return control;
  },
  avatar: node, tag: node, icon: node,
  deviceName: value => value,
  statusLabels: {unknown: 'Unknown'},
};
const employeeRow = vm.runInNewContext(`(${source.slice(start, end)})`, scope);
employeeRow(employee, {membership: true});
const remove = buttons.find(button => button.label === '移出项目');
assert(remove, 'member row must show Remove from project');

(async () => {
  await remove.events.click();
  assert.equal(removeCalls, 0, 'cancel must not send DELETE');
  assert.equal(remove.disabled, false, 'cancel must leave Remove usable');

  confirmRemoval = true;
  await remove.events.click();
  assert.equal(removeCalls, 1, 'a later confirmation must send DELETE exactly once');
  console.log('member removal cancel then confirm: PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
