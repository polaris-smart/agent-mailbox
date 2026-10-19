// Actual CLI, isolated home, local deterministic HTTP fixture. No model or account.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import readline from 'node:readline';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { connect } from './launcher.mjs';
import { rpc } from './client.mjs';

for(const transport of ['rpc','native']) test(`Desktop owns approval routing after ${transport} notification admission`,
  {skip: !process.env.TEST_CODEX_BINARY || (transport==='native' && !process.env.TEST_MAILBOX_PYTHON), timeout: 45000}, async t => {
    // Unix socket path limit includes the random launcher identity and suffix.
    const root = await fs.realpath(await fs.mkdtemp(path.join('/tmp', 'am-route-')));
    t.after(() => fs.rm(root, {recursive: true, force: true}));
    const base = path.join(root, 'ipc');
    const requests = [], desktopEvents = [], notifyEvents = [], errors = [];
    let fixtureError;
    const fixture = http.createServer(async (req, res) => {
      try {
        assert.equal(req.headers.authorization, undefined, 'fixture must receive no account credential');
        let body = ''; for await (const part of req) body += part;
        const input = JSON.parse(body); requests.push(input);
        assert.equal(requests.length, 1, 'do not continue inference after permission request');
        const tools = input.tools.flatMap(tool => tool.type === 'namespace' ? tool.tools : [tool]);
        const tool = tools.find(tool => ['exec_command', 'shell_command'].includes(tool.name));
        assert.ok(tool, 'installed CLI must offer a shell tool');
        const args = tool.name === 'exec_command'
          ? {cmd: 'echo MAILBOX_ROUTING_FIXTURE', sandbox_permissions: 'require_escalated', justification: 'Synthetic routing check; deny this request.'}
          : {command: 'echo MAILBOX_ROUTING_FIXTURE', sandbox_permissions: 'require_escalated', justification: 'Synthetic routing check; deny this request.'};
        const item = {type: 'function_call', id: 'fc_fixture', call_id: 'call_fixture', name: tool.name, arguments: JSON.stringify(args), status: 'completed'};
        res.writeHead(200, {'Content-Type': 'text/event-stream'});
        const send = event => res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
        send({type: 'response.created', response: {id: 'resp_fixture', status: 'in_progress', output: []}});
        send({type: 'response.output_item.added', output_index: 0, item: {...item, arguments: '', status: 'in_progress'}});
        send({type: 'response.function_call_arguments.delta', output_index: 0, item_id: item.id, delta: item.arguments});
        send({type: 'response.output_item.done', output_index: 0, item});
        send({type: 'response.completed', response: {id: 'resp_fixture', status: 'completed', output: [item], usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}}});
        res.end();
      } catch (e) { fixtureError = e; res.writeHead(500); res.end('fixture failed'); }
    });
    await new Promise((resolve, reject) => {
      fixture.once('error', reject); fixture.listen(0, '127.0.0.1', resolve);
    });
    t.after(() => {fixture.closeAllConnections(); fixture.close();});
    await fs.writeFile(path.join(root, 'config.toml'),
      `model_provider = "fixture"\nmodel = "fixture"\n[model_providers.fixture]\nname = "Local deterministic fixture"\nbase_url = "http://127.0.0.1:${fixture.address().port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n`);
    const env = {PATH: process.env.PATH, HOME: root, CODEX_HOME: root, TMPDIR: os.tmpdir(),
      AGENT_MAILBOX_CODEX_BINARY: process.env.TEST_CODEX_BINARY, AGENT_MAILBOX_CODEX_SOCKET_DIR: base};
    const mailboxHome=path.join(root,'mailbox');
    if(transport==='native') {
      await fs.mkdir(mailboxHome,{mode:0o700});
      env.AGENT_MAILBOX_CODEX_MAILBOX_HOME=mailboxHome;
    }
    const child = spawn(process.execPath, [new URL('./launcher.mjs', import.meta.url).pathname, 'app-server'],
      {env, cwd: root, stdio: ['pipe', 'pipe', 'pipe']});
    child.stderr.on('data', data => errors.push(data.toString()));
    const exited = new Promise(resolve => child.once('exit', resolve));
    t.after(async () => {child.kill('SIGTERM'); await exited; await fs.rm(root, {recursive: true, force: true});});
    let next = 0;
    const pending = new Map();
    child.once('exit',()=>{
      for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('isolated_backend_exited: '+errors.join('').slice(-1200)));}
      pending.clear();
    });
    const lines = readline.createInterface({input: child.stdout});
    const waitFor = async predicate => {
      const deadline = Date.now() + 15000;
      while (!predicate()) {
        if (fixtureError) throw fixtureError;
        if (Date.now() > deadline) throw Error(JSON.stringify({desktop: desktopEvents.map(m => m.method), notify: notifyEvents.map(m => m.method), stderr: errors.join('').slice(-1800)}));
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    };
    lines.on('line', line => {
      const m = JSON.parse(line);
      if (m.method) desktopEvents.push(m);
      else if (pending.has(m.id)) {const p = pending.get(m.id); pending.delete(m.id);clearTimeout(p.timer); m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result);}
    });
    const desktop = (method, params) => new Promise((resolve, reject) => {
      const id = ++next;
      const timer=setTimeout(()=>{pending.delete(id);reject(Error(method+' timeout: '+errors.join('').slice(-1200)));},15000);
      pending.set(id, {resolve, reject,timer}); child.stdin.write(JSON.stringify({id, method, params}) + '\n');
    });
    await desktop('initialize', {clientInfo: {name: 'mailbox_desktop_fixture', version: '1'}, capabilities: {experimentalApi: true}});
    child.stdin.write(JSON.stringify({method: 'initialized'}) + '\n');
    const thread = await desktop('thread/start', {ephemeral: false, cwd: root, approvalPolicy: 'on-request', approvalsReviewer: 'user', sandbox: 'workspace-write'});
    const file = (await fs.readdir(base)).find(n => n.endsWith('.json'));
    const manifest = JSON.parse(await fs.readFile(path.join(base, file)));
    if(transport==='rpc') {
      const ws = await connect(manifest.socket); t.after(() => ws.terminate());
      ws.on('message', raw => {const m = JSON.parse(raw); if (m.method) notifyEvents.push(m);});
      const notify = rpc(ws, 15000);
      await notify('initialize', {clientInfo: {name: 'mailbox_notification_fixture', version: '1'}, capabilities: {experimentalApi: true}});
      ws.send(JSON.stringify({method: 'initialized'}));
      const turn = await notify('turn/start', {threadId: thread.thread.id, input: [{type: 'text', text: 'Synthetic routing test.'}]});
      assert.ok(turn.turn.id);
      ws.close(); // Exactly the candidate notifier's admission-then-disconnect behavior.
    } else {
      assert.equal((await fs.stat(manifest.notification_socket)).mode&0o777,0o600);
      const binding={id:'binding_test',employee_id:'employee_test',project_id:'project_test',host_kind:'codex',host_session_id:thread.thread.id};
      const envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:'wake-native-test',employee_id:binding.employee_id,binding,fresh:['message_test']};
      await fs.mkdir(path.join(mailboxHome,'wake','wake-outbox'),{recursive:true});
      await fs.writeFile(path.join(mailboxHome,'wake','host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
      await fs.writeFile(path.join(mailboxHome,'wake','wake-outbox','wake-native-test.json'),JSON.stringify(envelope));
      const script='import json,sys\nfrom pathlib import Path\nfrom agent_mailbox.workbench_host_transport import deliver,_codex_health\nhome=Path(sys.argv[1]); endpoint=Path(sys.argv[2]); thread=sys.argv[3]\nassert _codex_health(endpoint,thread)\nassert not _codex_health(endpoint,"another-unloaded-thread")\ne=json.loads((home/"wake/wake-outbox/wake-native-test.json").read_text())\ndeliver(home,endpoint,e)\nprint((home/"wake/receipts/wake-native-test").read_text())';
      const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,['-c',script,mailboxHome,manifest.notification_socket,thread.thread.id],
        {env:{PATH:process.env.PATH,HOME:root,PYTHONPATH:path.resolve('src')},timeout:20000});
      assert.equal(JSON.parse(stdout).status,'received');
    }
    await waitFor(() => desktopEvents.some(m => m.id !== undefined && /requestApproval/.test(m.method)) ||
      desktopEvents.some(m => m.method === 'turn/completed'));
    const approval = desktopEvents.find(m => m.id !== undefined && /requestApproval/.test(m.method));
    assert.ok(approval, 'notification tool approval must arrive at Desktop after notifier disconnects');
    assert.equal(requests.length, 1);
    if(transport==='native')assert.ok(JSON.stringify(requests[0].input).includes('wake-native-test'));
    // Leave approval unanswered and stop the isolated backend; no command is approved.
  });
