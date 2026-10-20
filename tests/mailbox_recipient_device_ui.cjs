const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const employees = [
  {id: 'office', name: 'Codex', kind: 'codex', connection_type: 'cli', node_id: 'office-node', project_ids: ['p']},
  {id: 'home', name: 'Codex', kind: 'codex', connection_type: 'cli', node_id: 'home-node', project_ids: ['p']},
];
const state = {
  projectId: 'p', mailboxEmployeeId: 'office', mailboxFolder: 'inbox', mailboxMessageId: '',
  messageDrafts: {}, messages: {}, data: {
    employees, projects: [{id: 'p', name: 'Demo'}], messages: [],
    node: {id: 'office-node', name: 'Office Mac'},
    devices: [{id: 'office-node', name: 'Office Mac'}, {id: 'home-node', name: 'Home Mac'}],
  },
};
const el = (tag, props = {}, ...children) => ({
  tag, props, children: children.flat(Infinity).filter(x => x != null),
  checked: Boolean(props.checked), value: props.value || '',
  addEventListener() {}, append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
});
const context = {
  state, el, t: zh => zh, navLabels: {messages: '项目消息', mailboxes: '员工信箱'},
  heading: () => el('header'), formField: (_label, control) => control,
  errorBox: () => el('p'), button: () => el('button'), icon: () => '', avatar: () => '',
  formatDate: () => '', render: () => {},
};
const helpers = source.slice(source.indexOf('function project()'), source.indexOf('function formatDate('));
vm.runInNewContext(helpers, context);
const renderMessages = vm.runInNewContext(`(${source.slice(source.indexOf('function renderMessages()'), source.indexOf('\nconst governanceLabels', source.indexOf('function renderMessages()')))})`, context);
const renderMailboxes = vm.runInNewContext(`(${source.slice(source.indexOf('function renderMailboxes()'), source.indexOf('\nfunction renderMembers()', source.indexOf('function renderMailboxes()')))})`, context);

const form = renderMessages()[1].children[1];
const recipient = form.children.find(x => x.tag === 'select');
assert(recipient, 'recipient control should exist');
const choices = recipient.children.filter(x => x.props.value);
assert.equal(choices.length, 2);
assert.notEqual(choices[0].children.join(''), choices[1].children.join(''), 'same-name recipients need visible device context');
assert(choices[0].children.join('').includes('Office Mac'));
assert(choices[1].children.join('').includes('Home Mac'));

const rail = renderMailboxes()[1].children[0];
const first = rail.children[0].children[1].children[1].children.join('');
const second = rail.children[1].children[1].children[1].children.join('');
assert(first.includes('Office Mac') && second.includes('Home Mac'), 'mailbox rail must identify each device');

state.data.devices[1].name = 'Office Mac';
const duplicateNameChoices = renderMessages()[1].children[1].children.find(x => x.tag === 'select').children.filter(x => x.props.value);
assert.notEqual(duplicateNameChoices[0].children.join(''), duplicateNameChoices[1].children.join(''),
  'two devices with the same display name still need distinct recipient labels');
console.log('same-name recipients and mailbox rail: PASS');
