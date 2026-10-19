// Exercise the real manual-registration form and submit path with synthetic data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const start = source.indexOf('function openManualEmployeeForm()');
const end = source.indexOf('\n// 首次启动', start);
assert(start >= 0 && end > start, 'manual registration form must exist');

const fields = new Map();
const node = (tag = 'div', props = {}) => {
  const item = {
    tag, children: [], events: {}, value: props.value || '', checked: Boolean(props.checked),
    hidden: Boolean(props.hidden), disabled: Boolean(props.disabled),
    append(...children) { this.children.push(...children); },
    addEventListener(event, handler) { this.events[event] = handler; },
  };
  if (props.id) fields.set(props.id, item);
  return item;
};
let submitted = null;
let closed = false;
const body = node();
const scope = {
  t: value => value,
  el: (tag, props = {}, ...children) => { const item = node(tag, props); item.append(...children); return item; },
  openForm: () => body,
  project: () => ({id: 'project-isolated'}),
  state: {projectId: 'project-isolated'},
  errorBox: () => node('div', {hidden: true}),
  footer: () => ({node: node(), submit: node('button')}),
  api: {addEmployee: async payload => { submitted = payload; }},
  formDialog: {close: () => { closed = true; }},
  showToast: () => {},
  load: async () => {},
  formError: (_box, error) => { throw error; },
};
vm.runInNewContext(`${source.slice(start, end)}\nopenManualEmployeeForm()`, scope);
const form = body.children.find(child => child.tag === 'form');
assert(form, 'form must be mounted');
assert(form.children.every(child => child !== undefined), 'form must not render undefined placeholders');
fields.get('manual-employee-name').value = 'Synthetic employee';
fields.get('manual-employee-kind').value = 'aider';
fields.get('manual-employee-connection').value = 'cli';
fields.get('manual-employee-entry').value = '/usr/bin/true';

(async () => {
  await form.events.submit({preventDefault() {}});
  assert.equal(submitted.name, 'Synthetic employee');
  assert.equal(submitted.project_id, 'project-isolated');
  assert.equal(submitted.entrypoint, '/usr/bin/true');
  assert.equal(closed, true, 'successful submit must close the form');
  console.log('manual registration form submit: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
