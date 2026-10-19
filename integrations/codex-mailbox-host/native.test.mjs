import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {createReceiver} from './native.mjs';

async function fixture(t, outcome='received') {
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'am-native-codex-')));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const home=path.join(root,'home'), stateDir=path.join(root,'journal');
  const b={id:'binding_test',employee_id:'employee_test',project_id:'project_test',host_kind:'codex',host_session_id:'original-thread'};
  const envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:'wake-test',employee_id:b.employee_id,binding:b,fresh:['message_test']};
  await fs.mkdir(path.join(home,'wake','wake-outbox'),{recursive:true});
  const bindings=path.join(home,'wake','host-bindings.json'), outbox=path.join(home,'wake','wake-outbox','wake-test.json');
  await fs.writeFile(bindings,JSON.stringify({version:1,bindings:[b]}));
  await fs.writeFile(outbox,JSON.stringify(envelope));
  const calls=[];
  const candidates=async()=>[{close(){},async request(method,params){
    calls.push({method,params});
    if(method==='thread/loaded/list')return {data:outcome==='missing'?[]:[b.host_session_id]};
    if(method==='thread/read')return {thread:{id:b.host_session_id,ephemeral:false,status:{type:'idle'}}};
    assert.equal(method,'thread/queue/add');
    assert.equal(params.threadId,b.host_session_id);
    assert.deepEqual(Object.keys(params).sort(),['clientUserMessageId','input','threadId']);
    if(outcome==='lost')throw Error('lost_reply_after_admission');
    return {queuedSubmission:{id:'queued-test',clientUserMessageId:params.clientUserMessageId,input:params.input}};
  }}];
  const create=()=>createReceiver({home,stateDir,candidates});
  return {home,stateDir,bindings,outbox,envelope,calls,create};
}

test('duplicate and receiver restart reuse one exact durable admission',async t=>{
  const f=await fixture(t), receive=f.create();
  const r=await receive(f.envelope);
  assert.equal(r.status,'received');
  assert.deepEqual(await receive(f.envelope),r);
  assert.deepEqual(await f.create()(f.envelope),r);
  assert.equal(f.calls.filter(c=>c.method==='thread/queue/add').length,1);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.home,'wake','receipts','wake-test'),'utf8')),r);
});
test('concurrent copies cause at most one host admission',async t=>{
  const f=await fixture(t);
  const results=await Promise.allSettled([f.create()(f.envelope),f.create()(f.envelope)]);
  assert.ok(results.some(r=>r.status==='fulfilled' && r.value.status==='received'));
  assert.equal(f.calls.filter(c=>c.method==='thread/queue/add').length,1);
});
test('uncertain admission stays uncertain across restart and is not retried',async t=>{
  const f=await fixture(t,'lost');
  await assert.rejects(f.create()(f.envelope),/lost_reply/);
  await assert.rejects(f.create()(f.envelope),/prior_admission_unconfirmed/);
  assert.equal(f.calls.filter(c=>c.method==='thread/queue/add').length,1);
  await assert.rejects(fs.stat(path.join(f.home,'wake','receipts','wake-test')),/ENOENT/);
});
test('outbox mismatch, missing thread and revoked binding never start another thread',async t=>{
  const f=await fixture(t,'missing');
  await assert.rejects(f.create()({...f.envelope,fresh:['invented']}),/outbox_identity_mismatch/);
  assert.equal(f.calls.length,0);
  assert.equal((await f.create()(f.envelope)).status,'not_received');
  assert.equal(f.calls.filter(c=>c.method==='thread/queue/add').length,0);
  await fs.writeFile(f.bindings,JSON.stringify({version:1,bindings:[]}));
  assert.equal((await f.create()(f.envelope)).status,'not_received');
  assert.equal(f.calls.length,1);
});
test('revocation rejects even previously successful receipt; changed batch cannot reuse ID',async t=>{
  const f=await fixture(t);
  await f.create()(f.envelope);
  const changed={...f.envelope,fresh:['new-message']};
  await fs.writeFile(f.outbox,JSON.stringify(changed));
  await assert.rejects(f.create()(changed),/delivery_identity_conflict/);
  await fs.writeFile(f.outbox,JSON.stringify(f.envelope));
  await fs.writeFile(f.bindings,JSON.stringify({version:1,bindings:[]}));
  assert.equal((await f.create()(f.envelope)).status,'not_received');
  assert.equal(f.calls.filter(c=>c.method==='thread/queue/add').length,1);
});

// The control protocol must not become a general-purpose app-server proxy.
const {inspectSessions, inspectHealth, CONTROL_PROTOCOL, HEALTH_PROTOCOL}=await import('./native.mjs');
const query=(operation='sessions', extra={})=>({protocol:CONTROL_PROTOCOL,request_id:'synthetic',operation,...extra});
test('session discovery projects only loaded-session metadata; never content or execution',async()=>{
  const calls=[];
  const result=await inspectSessions(async(method,params)=>{
    calls.push({method,params});
    if(method==='thread/loaded/list')return {data:['thread-a','thread-a','thread-b'],nextCursor:'more'};
    assert.equal(method,'thread/read');assert.equal(params.includeTurns,false);
    return {thread:{id:params.threadId,name:'Synthetic name',cwd:'/synthetic/project',
      preview:'PRIVATE PROMPT',model:'PRIVATE MODEL',turns:[{secret:'PRIVATE TURN'}]}};
  },query());
  assert.deepEqual(result,{protocol:CONTROL_PROTOCOL,request_id:'synthetic',truncated:true,sessions:[
    {id:'thread-a',name:'Synthetic name',cwd:'/synthetic/project'},
    {id:'thread-b',name:'Synthetic name',cwd:'/synthetic/project'}]});
  assert.equal(calls.length,3);
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
});
test('inspect verifies loaded membership across pages and rejects an unloaded session',async()=>{
  let reads=0;
  const request=async(method,params)=>{
    if(method==='thread/loaded/list')return params.cursor?{data:['target']}:{data:['other'],nextCursor:'p2'};
    assert.equal(method,'thread/read');assert.equal(params.threadId,'target');reads++;
    return {thread:{id:'target',name:null,cwd:'/synthetic'}};
  };
  assert.equal((await inspectSessions(request,query('inspect',{thread_id:'target'}))).sessions[0].name,'');
  await assert.rejects(inspectSessions(request,query('inspect',{thread_id:'gone'})),/session_not_loaded/);
  assert.equal(reads,1);
});
test('health only confirms the specified loaded original thread without starting a turn',async()=>{
  const calls=[];
  const request=async(method,params)=>{
    calls.push(method);
    if(method==='thread/loaded/list')return {data:['target']};
    assert.equal(method,'thread/read');
    return {thread:{id:params.threadId,cwd:'/synthetic',name:'Private name',ephemeral:false,status:{type:'idle'}}};
  };
  const result=await inspectHealth(request,query('health',{thread_id:'target'}));
  assert.deepEqual(result,{protocol:HEALTH_PROTOCOL,request_id:'synthetic',thread_id:'target',
    loaded:true,admission_available:true});
  assert.deepEqual(calls,['thread/loaded/list','thread/read','thread/read']);
  const busy=await inspectHealth(async(method,params)=>method==='thread/loaded/list'
    ?{data:['target']}:{thread:{id:params.threadId,cwd:'/synthetic',ephemeral:false,status:{type:'active'}}},
    query('health',{thread_id:'target'}));
  assert.equal(busy.loaded,true);
  assert.equal(busy.admission_available,true);
  const ephemeral=await inspectHealth(async(method,params)=>method==='thread/loaded/list'
    ?{data:['target']}:{thread:{id:params.threadId,cwd:'/synthetic',ephemeral:true,status:{type:'idle'}}},
    query('health',{thread_id:'target'}));
  assert.equal(ephemeral.loaded,true);
  assert.equal(ephemeral.admission_available,false);
  await assert.rejects(inspectHealth(async()=>({data:[]}),query('health',{thread_id:'target'})),
    /session_not_loaded/);
  await assert.rejects(inspectHealth(()=>assert.fail('must not call host'),
    query('health',{thread_id:'../target'})),/invalid_receiver_control/);
});
test('unknown or injected operations never reach the backend',async()=>{
  const request=()=>assert.fail('must not query host');
  for(const q of [query('turn/start'),query('sessions',{thread_id:'x'}),query('inspect'),
    query('sessions',{params:{method:'turn/start'}}),query('inspect',{thread_id:'../x'})]) {
    await assert.rejects(inspectSessions(request,q),/invalid_receiver_control/);
  }
});
test('repeated cursors and mismatched metadata fail closed',async()=>{
  await assert.rejects(inspectSessions(async()=>({data:[],nextCursor:'repeat'}),query('inspect',{thread_id:'target'})),/repeated_loaded_cursor/);
  await assert.rejects(inspectSessions(async method=>method==='thread/loaded/list'?{data:['target']}:{thread:{id:'other',cwd:'/tmp'}},query()),/invalid_thread_metadata/);
});

const {inspectConfiguration}=await import('./native.mjs');
const configQuery=extra=>query('configuration',{thread_id:'target',...extra});
function configurationFixture(overrides={}) {
  const calls=[];
  const configuration={config:{model:'PRIVATE-MODEL',mcp_servers:{global_alias:{
    command:'/private/agent-mailbox',args:['--session-file','/private/SECRET.json'],env:{KEY:'PRIVATE-KEY'}}}},
    layers:[{name:{type:'user',file:'/private/USER-CONFIG'},config:{mcp_servers:{global_alias:{command:'mailbox-mcp'}}}},
      {name:{type:'project',dotCodexFolder:'/synthetic/.codex'},disabledReason:'PRIVATE-PATH trust instruction',
        config:{mcp_servers:{disabled_mailbox:{command:'mailbox-mcp',enabled:false}}}}]};
  const runtime={data:[{name:'opaque_wrapper',runtimeStatus:'connected',toolsError:null,
    serverInfo:{description:'PRIVATE-INSTRUCTIONS'},tools:{project_context:{description:'PRIVATE-CONTENT'},project_messages:{}}}]};
  const request=async(method,params)=>{
    calls.push({method,params});
    if(method==='thread/loaded/list')return {data:['target']};
    if(method==='thread/read')return {thread:{id:'target',cwd:'/synthetic'}};
    if(method==='config/read')return overrides.configuration??configuration;
    assert.equal(method,'mcpServerStatus/list');
    return typeof overrides.runtime==='function'?overrides.runtime(params):overrides.runtime??runtime;
  };
  return {calls,request,configuration,runtime};
}
test('configuration projects effective, disabled layer and runtime mailbox without secrets or execution',async()=>{
  const f=configurationFixture();
  const result=await inspectConfiguration(f.request,configQuery());
  assert.deepEqual(f.calls.map(c=>c.method),['thread/loaded/list','thread/read','config/read','mcpServerStatus/list']);
  assert.deepEqual(f.calls[2].params,{cwd:'/synthetic',includeLayers:true});
  assert.deepEqual(f.calls[3].params,{threadId:'target',detail:'toolsAndAuthOnly',limit:50});
  assert.equal(result.thread_id,'target');assert.equal(result.cwd,'/synthetic');
  assert.match(result.config_digest,/^[a-f0-9]{64}$/);
  assert.deepEqual(result.effective_servers,[{name:'global_alias',mailbox_like:true,enabled:true}]);
  assert.equal(result.layers[1].disabled,true);assert.equal(result.layers[1].workspace_layer,true);
  assert.equal(result.layers[1].servers[0].enabled,false);
  assert.deepEqual(result.runtime_servers,[{name:'opaque_wrapper',mailbox_like:true,connected:true,tools_available:true}]);
  assert.ok(!/PRIVATE|SECRET|session-file|command|args|description/.test(JSON.stringify(result)));
});
test('configuration rejects arbitrary cwd, methods and unloaded sessions',async()=>{
  const noRequest=()=>assert.fail('must reject before backend call');
  for(const q of [configQuery({cwd:'/elsewhere'}),configQuery({method:'turn/start'}),
      configQuery({thread_id:undefined}),configQuery({operation:'reload'})]) {
    await assert.rejects(inspectConfiguration(noRequest,q),/invalid_receiver_control/);
  }
  await assert.rejects(inspectConfiguration(async()=>({data:[]}),configQuery()),/session_not_loaded/);
});
test('configuration digest changes with mailbox parameters, not model settings',async()=>{
  const f=configurationFixture();
  const first=(await inspectConfiguration(f.request,configQuery())).config_digest;
  f.configuration.config.model='DIFFERENT-PRIVATE-MODEL';
  assert.equal((await inspectConfiguration(f.request,configQuery())).config_digest,first);
  f.configuration.config.mcp_servers.global_alias.args=['--session-file','/other.json'];
  assert.notEqual((await inspectConfiguration(f.request,configQuery())).config_digest,first);
});
test('unknown layers, malformed projections, overlarge config never mean no conflict',async()=>{
  for(const configuration of [
    {config:{},layers:null}, {config:{},layers:[{name:{type:'unknown'},config:{}}]},
    {config:{mcp_servers:[]},layers:[]}, {config:{mcp_servers:{a:{enabled:'false'}}},layers:[]},
    {config:{mcp_servers:{a:{env:{secret:'x'.repeat(262144)}}}},layers:[]},
    {config:{},layers:Array(33).fill({name:{type:'user'},config:{}})},
  ]) {
    const f=configurationFixture({configuration});
    await assert.rejects(inspectConfiguration(f.request,configQuery()),/invalid_config|config_size/);
  }
});
test('runtime repeated cursor, duplicate servers and too many pages fail closed',async()=>{
  for(const runtime of [
    ()=>({data:[],nextCursor:'repeat'}),
    ()=>({data:[{name:'a',tools:{}},{name:'a',tools:{}}]}),
    p=>({data:[],nextCursor:String(Number(p.cursor??0)+1)}),
  ]) {
    const f=configurationFixture({runtime});
    await assert.rejects(inspectConfiguration(f.request,configQuery()),/repeated_runtime_cursor|invalid_runtime_server|page_limit/);
    assert.ok(f.calls.length<=11);
  }
});
test('failed runtime discovery is explicitly unavailable, not an authenticated mailbox',async()=>{
  const f=configurationFixture({runtime:{data:[{name:'agent-mailbox-project',runtimeStatus:'failed',
    toolsError:'PRIVATE-FAILURE-DETAIL',tools:{}}]}});
  const result=await inspectConfiguration(f.request,configQuery());
  assert.deepEqual(result.runtime_servers,[{name:'agent-mailbox-project',mailbox_like:true,connected:false,tools_available:false}]);
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
});

const {inspectIdentity}=await import('./native.mjs');
function identityFixture() {
  const entry={args:['mailbox-mcp','--session-file','/private/synthetic.json'],command:'/synthetic/app'};
  const configuration={config:{mcp_servers:{'agent-mailbox-project':{...entry,
    environment_id:'local',enabled:true,tool_timeout_sec:null}}},layers:[{
    name:{type:'project',dotCodexFolder:'/synthetic/.codex'},config:{mcp_servers:{'agent-mailbox-project':entry}}}]};
  const runtime={data:[{name:'agent-mailbox-project',runtimeStatus:'connected',tools:{project_identity:{}}}]};
  const f=configurationFixture({configuration,runtime});
  const identity={protocol:'agent-mailbox.identity.v1',employee_id:'employee_test',project_id:'project_test',session_id:'mail_session_test'};
  const toolCalls=[];
  const result={content:[{type:'text',text:JSON.stringify(identity)}]};
  return {...f,configuration,runtime,identity,result,toolCalls,
    query:query('identity',{thread_id:'target',entry_digest:createHash('sha256').update(JSON.stringify(entry)).digest('hex')}),
    request:async(method,params)=>{
      if(method!=='mcpServer/tool/call')return f.request(method,params);
      toolCalls.push({method,params});return result;
    }};
}
test('configuration gives only an owned-entry digest, supports expired server metadata without calling its tool',async()=>{
  const f=identityFixture();
  f.runtime.data[0].runtimeStatus='failed';
  f.runtime.data[0].toolsError='PRIVATE expired credential';
  f.runtime.data[0].tools={};
  const result=await inspectConfiguration(f.request,configQuery());
  assert.equal(result.effective_servers[0].entry_digest,f.query.entry_digest);
  assert.equal(result.layers[0].servers[0].entry_digest,f.query.entry_digest);
  assert.equal(result.runtime_servers[0].connected,false);
  assert.equal(f.toolCalls.length,0);
  assert.ok(!/PRIVATE|synthetic.json|command|args/.test(JSON.stringify(result)));
  f.configuration.config.mcp_servers['agent-mailbox-project'].env={INJECT:'PRIVATE'};
  assert.notEqual((await inspectConfiguration(f.request,configQuery())).effective_servers[0].entry_digest,f.query.entry_digest);
  f.configuration.config.mcp_servers['agent-mailbox-project'].enabled=false;
  assert.equal((await inspectConfiguration(f.request,configQuery())).effective_servers[0].entry_digest,null);
});
test('identity calls only the fixed minimal diagnostic after scoped runtime/config checks',async()=>{
  const f=identityFixture(),result=await inspectIdentity(f.request,f.query);
  assert.deepEqual(result,{protocol:CONTROL_PROTOCOL,request_id:f.query.request_id,thread_id:'target',cwd:'/synthetic',identity:f.identity});
  assert.deepEqual(f.toolCalls,[{method:'mcpServer/tool/call',params:{threadId:'target',server:'agent-mailbox-project',tool:'project_identity',arguments:{}}}]);
  assert.ok(!JSON.stringify(result).includes('private'));
});
test('identity refuses arbitrary tool/method/args/digest before any host request',async()=>{
  const f=identityFixture();
  for(const extra of [{tool:'project_context'},{method:'turn/start'},{arguments:{x:1}},
      {cwd:'/other'},{entry_digest:'a'},{thread_id:undefined},{protocol:'other'},{request_id:''}]) {
    await assert.rejects(inspectIdentity(()=>assert.fail('no RPC allowed'),{...f.query,...extra}),/invalid_receiver_control/);
  }
});
test('identity rejects config overrides including defaults with changed meaning',async()=>{
  for(const change of [{command:'/other'}, {args:[]}, {enabled:false}, {environment_id:'remote'},
      {tool_timeout_sec:60},{env:{}},{cwd:'/other'},{unknown:true}]) {
    const f=identityFixture();Object.assign(f.configuration.config.mcp_servers['agent-mailbox-project'],change);
    await assert.rejects(inspectIdentity(f.request,f.query),/configuration_mismatch/);
    assert.deepEqual(f.toolCalls,[]);
  }
});
test('identity refuses aliases, global ownership, disabled project and old/disconnected MCP',async()=>{
  for(const kind of ['alias','global','disabled','runtime-alias','offline','no-tool','tool-error','missing-server']) {
    const f=identityFixture();
    if(kind==='alias')f.configuration.config.mcp_servers.other={command:'mailbox-mcp'};
    if(kind==='global')f.configuration.layers[0].name={type:'user'};
    if(kind==='disabled')f.configuration.layers[0].disabledReason='PRIVATE';
    if(kind==='runtime-alias')f.runtime.data.push({name:'opaque',runtimeStatus:'connected',tools:{project_context:{},project_messages:{}}});
    if(kind==='offline')f.runtime.data[0].runtimeStatus='disconnected';
    if(kind==='no-tool')f.runtime.data[0].tools={project_context:{}};
    if(kind==='tool-error')f.runtime.data[0].toolsError='PRIVATE';
    if(kind==='missing-server')f.runtime.data=[];
    await assert.rejects(inspectIdentity(f.request,f.query),/receiver_identity_/);
    assert.deepEqual(f.toolCalls,[]);
  }
});
test('identity returns no raw tool failure, content, malformed identities or secrets',async()=>{
  for(const value of [{isError:true,content:[{type:'text',text:'PRIVATE'}]}, {content:[]},
      {content:[{type:'text',text:'PRIVATE'}]}, {content:[{type:'text',text:'x'.repeat(8193)}]},
      {content:[{type:'text',text:JSON.stringify({protocol:'wrong'})}]},
      {content:[{type:'text',text:JSON.stringify({...identityFixture().identity,secret:'PRIVATE'})}]}]) {
    const f=identityFixture();Object.assign(f.result,value);
    await assert.rejects(inspectIdentity(f.request,f.query),e=>e.message==='receiver_identity_unconfirmed');
  }
  const f=identityFixture();
  await assert.rejects(inspectIdentity((m,p)=>m==='mcpServer/tool/call'?Promise.reject(Error('PRIVATE')):f.request(m,p),f.query),e=>e.message==='receiver_identity_unconfirmed');
});
