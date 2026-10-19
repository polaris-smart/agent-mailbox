import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {watch} from 'node:fs';
import path from 'node:path';
import { spawn, execFile } from 'node:child_process';
import {promisify} from 'node:util';
import readline from 'node:readline';
import { serverArguments, connect } from './launcher.mjs';
import { admit } from './client.mjs';

const binding={id:'binding_test',employee_id:'employee_test',project_id:'project_test',host_kind:'codex',host_session_id:'original-thread'};
const envelope=()=>({receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:'wake-test',employee_id:binding.employee_id,binding:{...binding},fresh:['message_test']});
test('only one already-loaded original thread receives notification without privilege overrides', async()=>{
  const calls=[];
  const client={close(){calls.push('close');},async request(method,params){calls.push({method,params});
    if(method==='thread/loaded/list')return {data:[binding.host_session_id]};
    if(method==='thread/read')return {thread:{id:binding.host_session_id,status:{type:'idle'}}};
    if(method==='thread/queue/add')return {queuedSubmission:{id:'queue_test',clientUserMessageId:params.clientUserMessageId,input:params.input}};
    assert.fail('unexpected API');
  }};
  const r=await admit(envelope(),{loadBindings:async()=>[binding],candidates:async()=>[client]});
  assert.equal(r.status,'received');
  const queued=calls.find(v=>v.method==='thread/queue/add').params;
  assert.deepEqual(Object.keys(queued).sort(),['clientUserMessageId','input','threadId']);
  assert.equal(queued.threadId,binding.host_session_id);
  assert.match(queued.input[0].text,/not a Human message/);
  assert.equal(calls.at(-1),'close');
});
test('busy original thread queues mail without steering its current turn',async()=>{
  const calls=[];
  const client={close(){},async request(method,params){
    calls.push(method);
    if(method==='thread/loaded/list')return {data:[binding.host_session_id]};
    if(method==='thread/read')return {thread:{id:params.threadId,status:{type:'active'}}};
    if(method==='thread/queue/add')return {queuedSubmission:{id:'queued_busy',clientUserMessageId:params.clientUserMessageId,input:params.input}};
    assert.fail('busy thread must not receive turn/start');
  }};
  const result=await admit(envelope(),{loadBindings:async()=>[binding],candidates:async()=>[client]});
  assert.equal(result.status,'received');
  assert.deepEqual(calls,['thread/loaded/list','thread/read','thread/queue/add']);
});
test('missing, duplicated, revoked, or changed bindings never start or resume a thread', async()=>{
  for(const mode of ['missing','two','revoked','changed']) {
    let reads=0;
    const client={close(){},async request(m){assert.equal(m,'thread/loaded/list');return {data:mode==='missing'?[]:[binding.host_session_id]};}};
    const r=await admit(envelope(),{loadBindings:async()=>++reads>1&&mode==='changed'?[]:mode==='revoked'?[]:[binding],
      candidates:async()=>mode==='two'?[client,client]:[client]});
    assert.equal(r.status,'not_received',mode);
  }
});
test('uncertain turn admission never becomes a success or negative receipt', async()=>{
  let closed=false;
  const client={close(){closed=true;},async request(m){if(m==='thread/loaded/list')return {data:[binding.host_session_id]};
    if(m==='thread/read')return {thread:{id:binding.host_session_id,status:{type:'idle'}}};throw Error('lost response');}};
  await assert.rejects(admit(envelope(),{loadBindings:async()=>[binding],candidates:async()=>[client]}),/lost response/);
  assert.equal(closed,true);
});

test('loaded-thread discovery has a page bound and rejects repeated cursors before delivery',async()=>{
  for(const mode of ['repeated','limit','cycle']) {
    let calls=0,closed=false;
    const client={close(){closed=true;},async request(method,params){
      assert.equal(method,'thread/loaded/list');calls++;
      return {data:[],nextCursor:mode==='repeated'?'same':mode==='cycle'?String(calls%2):String(calls)};
    }};
    await assert.rejects(admit(envelope(),{loadBindings:async()=>[binding],candidates:async()=>[client]}),/repeated_loaded_cursor|loaded_thread_limit/);
    assert.equal(calls,mode==='repeated'?2:mode==='cycle'?3:20);assert.equal(closed,true);
  }
});

test('invalid loaded-thread page cannot smuggle a target into admission',async()=>{
  for(const page of [
    {data:[binding.host_session_id],nextCursor:1},
    {data:[binding.host_session_id],nextCursor:''},
    {data:[binding.host_session_id],nextCursor:'x'.repeat(4097)},
    {data:[binding.host_session_id,{}]},
    {data:[binding.host_session_id,binding.host_session_id]},
    {data:[binding.host_session_id,...Array.from({length:100},(_,i)=>`thread_${i}`)]},
  ]) {
    let closed=false;
    const client={close(){closed=true;},async request(method){assert.equal(method,'thread/loaded/list');return page;}};
    await assert.rejects(admit(envelope(),{loadBindings:async()=>[binding],candidates:async()=>[client]}),/invalid_loaded_threads/);
    assert.equal(closed,true);
  }
});

test('target on final permitted page still delivers once, unless binding was revoked',async()=>{
  for(const revoked of [false,true]) {
    let pages=0,turns=0,reads=0,closed=false;
    const client={close(){closed=true;},async request(method,params){
      if(method==='thread/loaded/list')return {data:++pages===20?[binding.host_session_id]:[],nextCursor:String(pages)};
      if(method==='thread/read')return {thread:{id:binding.host_session_id,status:{type:'idle'}}};
      assert.equal(method,'thread/queue/add');turns++;return {queuedSubmission:{id:'queue_test',clientUserMessageId:params.clientUserMessageId,input:params.input}};
    }};
    const result=await admit(envelope(),{loadBindings:async()=>++reads>1&&revoked?[]:[binding],candidates:async()=>[client]});
    assert.equal(result.status,revoked?'not_received':'received');
    assert.equal(turns,revoked?0:1);assert.equal(pages,20);assert.equal(closed,true);
  }
});

test('launcher changes only a normal app-server stdio transport', () => {
  assert.deepEqual(serverArguments(['-c','feature=true','app-server','-c','plugin=true']),
    ['-c','feature=true','app-server','-c','plugin=true']);
  assert.deepEqual(serverArguments(['app-server','--listen','stdio://']), ['app-server']);
  for (const a of [['--version'],['exec','resume','id'],['app-server','proxy'],
    ['app-server','daemon','start'],['app-server','--help'],['app-server','--listen','unix:///elsewhere']]) {
    assert.equal(serverArguments(a), null);
  }
});

function requests(send, subscribe) {
  let next = 0;
  const pending = new Map();
  subscribe(message => {
    const r = pending.get(message.id);
    if (r) { pending.delete(message.id); clearTimeout(r.timer);
      message.error ? r.reject(new Error(JSON.stringify(message.error))) : r.resolve(message.result); }
  });

  return (method, params = {}, notification = false) => {
    if (notification) return send({method, params});
    const id = ++next;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {pending.delete(id); reject(new Error(method+' timed out'));},15000);
      pending.set(id,{resolve,reject,timer});send({id,method,params});
    });
  };
}

test('two real CLI launches resolve new receiver without changing owner endpoint config',
  {skip:!process.env.TEST_CODEX_BINARY || !process.env.TEST_MAILBOX_PYTHON,timeout:45000}, async t=>{
    const root=await fs.realpath(await fs.mkdtemp('/tmp/am-restart-'));
    const base=path.join(root,'ipc'), home=path.join(root,'mailbox');
    await fs.mkdir(path.join(home,'wake'),{recursive:true,mode:0o700});
    const binding={id:'binding_a',employee_id:'employee_a',project_id:'project_a',
      host_kind:'codex',host_session_id:'original-thread'};
    const config=JSON.stringify({version:1,endpoints:[{binding_id:binding.id,
      employee_id:binding.employee_id,host_kind:'codex',launcher_directory:base}]});
    await fs.writeFile(path.join(home,'wake/host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
    await fs.writeFile(path.join(home,'wake/host-endpoints.json'),config,{mode:0o600});
    t.after(()=>fs.rm(root,{recursive:true,force:true}));
    const paths=[];
    for(let round=0;round<2;round++) {
      const child=spawn(process.execPath,[new URL('./launcher.mjs',import.meta.url).pathname,'app-server'],{
        env:{PATH:process.env.PATH,HOME:root,CODEX_HOME:root,
          AGENT_MAILBOX_CODEX_BINARY:process.env.TEST_CODEX_BINARY,
          AGENT_MAILBOX_CODEX_SOCKET_DIR:base,AGENT_MAILBOX_CODEX_MAILBOX_HOME:home},
        stdio:['pipe','pipe','pipe']});
      child.stderr.resume();
      const exited=new Promise(resolve=>child.once('exit',resolve));
      const lines=readline.createInterface({input:child.stdout});
      const desktop=requests(m=>child.stdin.write(JSON.stringify(m)+'\n'),f=>lines.on('line',l=>f(JSON.parse(l))));
      try {
        await desktop('initialize',{clientInfo:{name:'mailbox_restart_fixture',version:'1'},capabilities:{experimentalApi:true}});
        desktop('initialized',{},true);
        const script='import json,sys; from pathlib import Path; from agent_mailbox.workbench_host_transport import endpoint_for; h=Path(sys.argv[1]); b=json.loads((h/"wake/host-bindings.json").read_text())["bindings"][0]; print(endpoint_for(h,b))';
        const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,['-c',script,home],{
          env:{PATH:process.env.PATH,PYTHONPATH:new URL('../../src',import.meta.url).pathname},timeout:5000});
        const endpoint=stdout.trim();
        assert.ok(endpoint.startsWith(base+'/') && endpoint.endsWith('.mail.sock'));
        assert.equal((await fs.stat(endpoint)).isSocket(),true);
        assert.equal(await fs.readFile(path.join(home,'wake/host-endpoints.json'),'utf8'),config);
        paths.push(endpoint);
      } finally {
        child.stdin.end();
        if(child.exitCode===null && child.signalCode===null)child.kill('SIGTERM');
        await exited;
        lines.close();
      }
      assert.deepEqual((await fs.readdir(base)).filter(n=>n.endsWith('.json') || n.endsWith('.sock')),[]);
    }
    assert.notEqual(paths[0],paths[1]);
  });

for (const native of [false,true]) for (const shutdown of ['stdin-end', 'SIGTERM']) test(`real installed Codex: shared backend, native=${native}, ${shutdown} cleanup`,
  {skip: !process.env.TEST_CODEX_BINARY, timeout: 45000}, async t => {
    const root = await fs.realpath(await fs.mkdtemp(path.join('/tmp', 'am-codex-')));
    const base = path.join(root,'ipc');
    if(native)await fs.mkdir(path.join(root,'mailbox'),{mode:0o700});
    const processExit = [];
    const child=spawn(process.execPath, [new URL('./launcher.mjs',import.meta.url).pathname,'app-server'], {
      env:{...process.env, CODEX_HOME:root, AGENT_MAILBOX_CODEX_BINARY:process.env.TEST_CODEX_BINARY,
        AGENT_MAILBOX_CODEX_SOCKET_DIR:base, AGENT_MAILBOX_CODEX_MAILBOX_HOME:native?path.join(root,'mailbox'):''}, stdio:['pipe','pipe','pipe'],
    });
    child.stderr.resume();
    const exited=new Promise(resolve=>child.once('exit', code=>{processExit.push(code);resolve();}));
    t.after(async()=>{if(!processExit.length) child.kill('SIGTERM');await exited;await fs.rm(root,{recursive:true,force:true});});
    const lines=readline.createInterface({input:child.stdout});
    const desktop=requests(m=>child.stdin.write(JSON.stringify(m)+'\n'), f=>lines.on('line',l=>f(JSON.parse(l))));
    await desktop('initialize',{clientInfo:{name:'mailbox_desktop_transport_test',version:'1'},capabilities:{experimentalApi:true}});
    desktop('initialized',{},true);
    const manifests=(await fs.readdir(base)).filter(n=>n.endsWith('.json'));
    assert.equal(manifests.length,1);
    const manifest=JSON.parse(await fs.readFile(path.join(base,manifests[0])));
    assert.equal((await fs.stat(base)).mode&0o777,0o700);
    assert.equal((await fs.stat(manifest.socket)).mode&0o777,0o600);
    if(native) {
      assert.equal((await fs.stat(manifest.notification_socket)).mode&0o777,0o600);
      assert.equal(manifest.mailbox_home,path.join(root,'mailbox'));
    }
    const ws=await connect(manifest.socket); t.after(()=>ws.terminate());
    const notify=requests(m=>ws.send(JSON.stringify(m)),f=>ws.on('message',d=>f(JSON.parse(d))));
    await notify('initialize',{clientInfo:{name:'mailbox_notification_transport_test',version:'1'},capabilities:{experimentalApi:true}});
    notify('initialized',{},true);
    const created=await desktop('thread/start',{ephemeral:true,cwd:root});
    assert.ok(created.thread.id);
    const loaded=await notify('thread/loaded/list');
    assert.ok(loaded.data.includes(created.thread.id),'notification client must see Desktop-owned thread');
    ws.close();
    if (shutdown === 'SIGTERM') child.kill('SIGTERM'); else child.stdin.end();
    await exited;
    assert.equal(processExit[0],0);
    assert.deepEqual(await fs.readdir(base),[],'shutdown removes endpoint and manifest');
  });

test('stopping while the native receiver is being published leaves no listener or manifest',
  {skip:!process.env.TEST_CODEX_BINARY,timeout:20000},async t=>{
    const root=await fs.realpath(await fs.mkdtemp('/tmp/am-stop-'));
    await fs.mkdir(path.join(root,'mailbox'),{mode:0o700});
    const base=path.join(root,'ipc');await fs.mkdir(base,{mode:0o700});
    let child, signaled=false;
    const watcher=watch(base,(_event,name)=>{
      if(!signaled && String(name).endsWith('.mail.sock')){signaled=true;child.kill('SIGTERM');}
    });
    t.after(()=>watcher.close());
    child=spawn(process.execPath,[new URL('./launcher.mjs',import.meta.url).pathname,'app-server'],{
      env:{PATH:process.env.PATH,HOME:root,CODEX_HOME:root,
        AGENT_MAILBOX_CODEX_BINARY:process.env.TEST_CODEX_BINARY,
        AGENT_MAILBOX_CODEX_SOCKET_DIR:base,AGENT_MAILBOX_CODEX_MAILBOX_HOME:path.join(root,'mailbox')},
      stdio:['pipe','pipe','pipe'],
    });
    child.stdout.resume();child.stderr.resume();
    const exited=new Promise(resolve=>child.once('exit',resolve));
    t.after(async()=>{if(child.exitCode===null && child.signalCode===null)child.kill('SIGTERM');await exited;await fs.rm(root,{recursive:true,force:true});});
    await exited;
    assert.equal(signaled,true);
    assert.deepEqual(await fs.readdir(base),[]);
  });

test('real isolated CLI session discovery crosses Python and native IPC without a turn',
  {skip:!process.env.TEST_CODEX_BINARY || !process.env.TEST_MAILBOX_PYTHON,timeout:30000}, async t=>{
    const root=await fs.realpath(await fs.mkdtemp('/tmp/am-sessions-'));
    const base=path.join(root,'ipc'),home=path.join(root,'mailbox');
    await fs.mkdir(home,{mode:0o700});
    const child=spawn(process.execPath,[new URL('./launcher.mjs',import.meta.url).pathname,'app-server'],{
      env:{PATH:process.env.PATH,HOME:root,CODEX_HOME:root,AGENT_MAILBOX_CODEX_BINARY:process.env.TEST_CODEX_BINARY,
        AGENT_MAILBOX_CODEX_SOCKET_DIR:base,AGENT_MAILBOX_CODEX_MAILBOX_HOME:home},
      cwd:root,stdio:['pipe','pipe','pipe']});
    child.stderr.resume();
    const exited=new Promise(resolve=>child.once('exit',resolve));
    const lines=readline.createInterface({input:child.stdout});
    t.after(async()=>{child.stdin.end();await exited;lines.close();await fs.rm(root,{recursive:true,force:true});});
    const desktop=requests(m=>child.stdin.write(JSON.stringify(m)+'\n'),cb=>lines.on('line',l=>cb(JSON.parse(l))));
    await desktop('initialize',{clientInfo:{name:'mailbox_session_fixture',version:'1'},capabilities:{experimentalApi:true}});
    await desktop('initialized',{},true);
    const file=(await fs.readdir(base)).find(n=>n.endsWith('.json'));
    const manifest=JSON.parse(await fs.readFile(path.join(base,file),'utf8'));
    const inspect=async id=>{
      const source='import json,sys; from agent_mailbox.workbench_codex_connection import _sessions; print(json.dumps(_sessions(sys.argv[1], sys.argv[2] if len(sys.argv)>2 else None)))';
      const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,['-c',source,manifest.notification_socket,...id?[id]:[]],
        {env:{PATH:process.env.PATH,PYTHONPATH:path.resolve('src')},timeout:12000});
      return JSON.parse(stdout);
    };
    assert.deepEqual(await inspect(),{sessions:[],truncated:false});
    // Seed one synthetic empty thread, no turn or model request; onboarding itself never creates it.
    const {thread}=await desktop('thread/start',{ephemeral:true,cwd:root,approvalPolicy:'never',sandbox:'read-only'});
    const result=await inspect();
    assert.equal(result.sessions.length,1);assert.equal(result.sessions[0].id,thread.id);
    assert.equal(result.sessions[0].cwd,root);
    assert.deepEqual(Object.keys(result.sessions[0]).sort(),['cwd','id','name']);
    assert.deepEqual((await inspect(thread.id)).sessions,result.sessions);
    await assert.rejects(inspect('not-loaded'));
    const original=await desktop('thread/read',{threadId:thread.id,includeTurns:false});
    assert.equal(original.thread.id,thread.id);
    assert.equal(original.thread.turns.length,0);
    await assert.rejects(fs.stat(path.join(base,'deliveries')),/ENOENT/);
    await assert.rejects(fs.stat(path.join(home,'wake')),/ENOENT/);
  });
