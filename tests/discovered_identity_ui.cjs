const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_mailbox/workbench_assets/workbench.js'), 'utf8');
const start = source.indexOf('function suggestedDiscoveredIdentity(');
const end = source.indexOf('\nfunction openProjectForm(', start);
assert(start >= 0 && end > start);
const suggest = vm.runInNewContext(`(${source.slice(start, end)})`);

const cli = {name: 'Codex', kind: 'codex', connection_type: 'cli', entrypoint: '/opt/bin/codex'};
const app = {name: 'Codex', kind: 'codex', connection_type: 'app', entrypoint: '/Applications/Codex.app'};
const renamedCli = {name: 'Codex CLI', connection_type: 'cli', entrypoint: '/opt/bin/codex'};

assert.equal(suggest(cli, [], [cli, app]).suggestedName, 'Codex CLI',
  'simultaneous CLI and App discoveries need distinct default mailbox names');
assert.equal(suggest(app, [], [cli, app]).suggestedName, 'Codex App');

let result = suggest(cli, [renamedCli]);
assert.equal(result.sameInstance.name, 'Codex CLI');
assert.equal(result.suggestedName, 'Codex CLI', 'rediscovery must retain the existing mailbox name');

result = suggest(app, [renamedCli]);
assert.equal(result.sameInstance, undefined);
assert.equal(result.suggestedName, 'Codex', 'independent app can use the unclaimed name');

result = suggest(app, [{name: 'Codex', connection_type: 'cli', entrypoint: '/opt/bin/codex'}]);
assert.equal(result.sameInstance, undefined);
assert.equal(result.suggestedName, 'Codex App', 'app must not reuse the CLI mailbox name');

result = suggest(cli, [
  {name: 'Codex', connection_type: 'app', entrypoint: '/Applications/Codex.app'},
  {name: 'Codex CLI', connection_type: 'cli', entrypoint: '/other/codex'},
]);
assert.equal(result.sameInstance, undefined);
assert.equal(result.suggestedName, 'Codex CLI 2', 'another CLI entrypoint needs a free identity name');

console.log('discovered identity naming: PASS');

// Exercise the rendered discovery controls as well as the naming helper.
const formStart = source.indexOf('async function openEmployeeForm()');
const formEnd = source.indexOf('\nfunction openTaskForm(', formStart);
assert(formStart >= 0 && formEnd > formStart);
const inputs = [];
const buttons = [];
const element = (tag = 'div', props = {}, ...children) => {
  const item = {
    tag, children: [...children], events: {}, value: props.value || '',
    hidden: Boolean(props.hidden), disabled: Boolean(props.disabled),
    isConnected: true, textContent: '',
    append(...next) { this.children.push(...next); },
    replaceChildren(...next) { this.children = [...next]; },
    addEventListener(name, handler) { this.events[name] = handler; },
  };
  if (tag === 'input') inputs.push(item);
  return item;
};
const formBody = element();
const context = {
  state: {data: {node: {id: 'local-node'}, employees: [
    {name: 'Codex CLI', kind: 'codex', node_id: 'local-node', connection_type: 'cli',
      entrypoint: '/opt/bin/codex', lifecycle: 'active'},
  ]}},
  formDialog: {open: true},
  openForm: () => formBody,
  el: element,
  button(label) { const item = element('button'); item.textContent = label; buttons.push(item); return item; },
  errorBox: () => element('div', {hidden: true}),
  formField: (_label, control) => element('div', {}, control),
  avatar: () => element(), tag: () => element(),
  t: value => value, humanDetail: value => value,
  api: {discoverEmployees: async () => ({employees: [cli, app]})},
  formError: (_box, error) => { throw error; },
};
vm.runInNewContext(`${source.slice(start, end)}\n${source.slice(formStart, formEnd)}\nopenEmployeeForm()`, context);
setImmediate(() => {
  assert.equal(inputs.length, 2);
  assert.equal(inputs[0].value, 'Codex CLI');
  assert.equal(inputs[1].value, 'Codex App');
  assert.equal(buttons[1].textContent, '已登记');
  assert.equal(buttons[1].disabled, true);
  assert.equal(buttons[2].textContent, '登记员工');
  assert.equal(buttons[2].disabled, false);
  console.log('discovered identity form: PASS');
});
