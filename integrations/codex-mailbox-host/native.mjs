// Same-backend receiver for the workbench's standard Unix notification sender.
import fs from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {admit, deliveryReceipt, privatePath, rpc, threadQueueAvailable, writeReceipt} from './client.mjs';
import {connect} from './socket.mjs';

async function syncDirectory(directory) {
  const handle=await fs.open(directory,'r');
  try {await handle.sync();} finally {await handle.close();}
}

export const CONTROL_PROTOCOL='agent-mailbox.receiver-control.v1';
export const HEALTH_PROTOCOL='agent-mailbox.receiver-health.v1';

// The sender may only treat this exact, already-loaded original thread as ready.
// No turn is started and no mailbox content is read by this diagnostic.
export async function inspectHealth(request, query) {
  if(query?.protocol!==CONTROL_PROTOCOL || query.operation!=='health' ||
      typeof query.request_id!=='string' || !/^[A-Za-z0-9_-]{1,128}$/.test(query.request_id) ||
      typeof query.thread_id!=='string' || !/^[A-Za-z0-9_-]{1,128}$/.test(query.thread_id) ||
      Object.keys(query).some(k=>!['protocol','request_id','operation','thread_id'].includes(k))) {
    throw Error('invalid_receiver_control');
  }
  const inspected=await inspectSessions(request,{...query,operation:'inspect'});
  if(inspected.sessions.length!==1 || inspected.sessions[0].id!==query.thread_id) {
    throw Error('session_not_loaded');
  }
  const available=await threadQueueAvailable(request,query.thread_id);
  return {protocol:HEALTH_PROTOCOL,request_id:query.request_id,
    thread_id:query.thread_id,loaded:true,admission_available:available};
}

// Owner-facing discovery only: no turn, resume, approval, preview or model data.
export async function inspectSessions(request, query) {
  const valid=v=>typeof v==='string' && /^[A-Za-z0-9_-]{1,128}$/.test(v);
  if(query?.protocol!==CONTROL_PROTOCOL || !valid(query.request_id) ||
      !['sessions','inspect'].includes(query.operation) ||
      Object.keys(query).some(k=>!['protocol','request_id','operation','thread_id'].includes(k)) ||
      (query.operation==='inspect' ? !valid(query.thread_id) : query.thread_id!==undefined)) {
    throw Error('invalid_receiver_control');
  }
  let cursor, ids=[], truncated=false;
  const seen=new Set();
  for(let pageIndex=0;pageIndex<20;pageIndex++) {
    const page=await request('thread/loaded/list',{limit:50,...cursor?{cursor}:{}});
    if(!Array.isArray(page?.data) || page.data.length>50 || page.data.some(id=>!valid(id)) ||
        (page.nextCursor!=null && (typeof page.nextCursor!=='string' || !page.nextCursor))) {
      throw Error('invalid_loaded_threads');
    }
    if(query.operation==='sessions') {ids=[...new Set(page.data)];truncated=!!page.nextCursor;break;}
    if(page.data.includes(query.thread_id)) {ids=[query.thread_id];break;}
    if(!page.nextCursor)break;
    if(seen.has(page.nextCursor))throw Error('repeated_loaded_cursor');
    seen.add(page.nextCursor);cursor=page.nextCursor;
    if(pageIndex===19)throw Error('loaded_thread_limit');
  }
  if(query.operation==='inspect' && !ids.length)throw Error('session_not_loaded');
  const sessions=[];
  for(const id of ids) {
    const {thread}=await request('thread/read',{threadId:id,includeTurns:false});
    if(thread?.id!==id || typeof thread.cwd!=='string' || !path.isAbsolute(thread.cwd)) {
      throw Error('invalid_thread_metadata');
    }
    sessions.push({id,name:typeof thread.name==='string'?thread.name.slice(0,200):'',cwd:thread.cwd.slice(0,4096)});
  }
  return {protocol:CONTROL_PROTOCOL,request_id:query.request_id,sessions,truncated};
}

function mailboxLike(value) {
  return /agent[-_]mailbox|mailbox-mcp|--session-file/i.test(JSON.stringify(value));
}

function serverProjection(servers) {
  if(!servers || typeof servers!=='object' || Array.isArray(servers) || Object.keys(servers).length>64) {
    throw Error('invalid_config_servers');
  }
  return Object.entries(servers).map(([name,entry])=>{
    if(!name || name.length>160 || /[\x00-\x1f\x7f]/.test(name) || !entry ||
        typeof entry!=='object' || Array.isArray(entry) ||
        (entry.enabled!==undefined && typeof entry.enabled!=='boolean'))throw Error('invalid_config_server');
    return {name,mailbox_like:mailboxLike([name,entry.command,entry.args]),enabled:entry.enabled!==false,
      ...(name==='agent-mailbox-project'?{entry_digest:ownedEntryDigest(entry)}:{})};
  });
}

function canonical(value, depth=0) {
  if(depth>20)throw Error('config_depth_limit');
  if(Array.isArray(value))return '['+value.map(v=>canonical(v,depth+1)).join(',')+']';
  if(value && typeof value==='object')return '{'+Object.keys(value).sort().map(
    key=>JSON.stringify(key)+':'+canonical(value[key],depth+1)).join(',')+'}';
  return JSON.stringify(value);
}

function ownedEntryDigest(entry) {
  // Observed config/read defaults in the supported CLI. Do not erase env,
  // cwd, remote environment, timeout overrides or future unknown fields.
  if(!entry || typeof entry!=='object' || Array.isArray(entry))return null;
  const copy={...entry};
  for(const [key,value] of Object.entries({environment_id:'local',enabled:true,tool_timeout_sec:null})) {
    if(Object.hasOwn(copy,key)) {
      if(copy[key]!==value)return null;
      delete copy[key];
    }
  }
  return createHash('sha256').update(canonical(copy)).digest('hex');
}

// A bounded diagnostic, never a generic RPC or config proxy. Do not return
// arguments/env/tool descriptions: they may contain secrets or private content.
export async function inspectConfiguration(request, query) {
  if(query?.operation!=='configuration')throw Error('invalid_receiver_control');
  return configuration(request,query);
}

async function configuration(request, query, ownedDigest) {
  const discovered=await inspectSessions(request,{...query,operation:'inspect'});
  const {id:threadId,cwd}=discovered.sessions[0];
  if(cwd.length>=4096)throw Error('invalid_thread_metadata');
  const read=await request('config/read',{cwd,includeLayers:true});
  if(!read?.config || typeof read.config!=='object' || Array.isArray(read.config) ||
      !Array.isArray(read.layers) || read.layers.length>32)throw Error('invalid_config_layers');
  const effective=read.config.mcp_servers??{};
  const effective_servers=serverProjection(effective);
  const fingerprints=[effective];
  const layerTypes=new Set(['packagedDefaults','mdm','system','enterpriseManaged','user','project',
    'sessionFlags','legacyManagedConfigTomlFromFile','legacyManagedConfigTomlFromMdm']);
  const layers=read.layers.map(layer=>{
    if(!layerTypes.has(layer?.name?.type) || !layer.config || typeof layer.config!=='object' ||
        Array.isArray(layer.config) || (layer.disabledReason!=null && typeof layer.disabledReason!=='string') ||
        (layer.name.type==='project' && (typeof layer.name.dotCodexFolder!=='string' ||
          !path.isAbsolute(layer.name.dotCodexFolder))))throw Error('invalid_config_layer');
    const servers=layer.config.mcp_servers??{};
    const scope=layer.name.type, disabled=layer.disabledReason!=null;
    const workspace_layer=scope==='project' && path.normalize(layer.name.dotCodexFolder)===path.join(cwd,'.codex');
    fingerprints.push({scope,disabled,workspace_layer,servers});
    return {scope,disabled,workspace_layer,servers:serverProjection(servers)};
  });
  if(Buffer.byteLength(JSON.stringify(fingerprints))>262144)throw Error('config_size_limit');
  if(ownedDigest!==undefined) {
    const owned=effective['agent-mailbox-project'];
    if(ownedEntryDigest(owned)!==ownedDigest ||
        effective_servers.some(s=>s.mailbox_like && s.name!=='agent-mailbox-project') ||
        layers.some(layer=>(layer.workspace_layer && layer.disabled) || layer.servers.some(s=>
          (s.mailbox_like || s.name==='agent-mailbox-project') &&
          (!layer.workspace_layer || layer.disabled || s.name!=='agent-mailbox-project')))) {
      throw Error('receiver_identity_configuration_mismatch');
    }
  }
  const runtime_servers=[], seen=new Set();
  let identityAvailable=false;
  let cursor;
  for(let pageIndex=0;pageIndex<8;pageIndex++) {
    const page=await request('mcpServerStatus/list',{threadId,detail:'toolsAndAuthOnly',limit:50,...cursor?{cursor}:{}});
    if(!Array.isArray(page?.data) || page.data.length>50 || runtime_servers.length+page.data.length>64 ||
        (page.nextCursor!=null && (typeof page.nextCursor!=='string' || !page.nextCursor)))throw Error('invalid_runtime_servers');
    for(const row of page.data) {
      if(typeof row?.name!=='string' || !row.name || row.name.length>160 || /[\x00-\x1f\x7f]/.test(row.name) ||
          runtime_servers.some(s=>s.name===row.name) || !row.tools || typeof row.tools!=='object' ||
          Array.isArray(row.tools) || (row.toolsError!=null && typeof row.toolsError!=='string'))throw Error('invalid_runtime_server');
      runtime_servers.push({name:row.name,mailbox_like:mailboxLike(row.name) ||
        (Object.hasOwn(row.tools,'project_context') && Object.hasOwn(row.tools,'project_messages')),
        connected:row.runtimeStatus==='connected',tools_available:row.toolsError==null});
      if(row.name==='agent-mailbox-project')identityAvailable=Object.hasOwn(row.tools,'project_identity');
    }
    if(!page.nextCursor)break;
    if(seen.has(page.nextCursor))throw Error('repeated_runtime_cursor');
    seen.add(page.nextCursor);cursor=page.nextCursor;
    if(pageIndex===7)throw Error('runtime_server_page_limit');
  }
  if(ownedDigest!==undefined) {
    const own=runtime_servers.find(s=>s.name==='agent-mailbox-project');
    if(!own?.connected || !own.tools_available || !identityAvailable ||
        runtime_servers.some(s=>!s.connected || !s.tools_available || (s.mailbox_like && s.name!==own.name)) ||
        effective_servers.some(s=>s.enabled && !runtime_servers.some(r=>r.name===s.name))) {
      throw Error('receiver_identity_host_not_ready');
    }
  }
  return {protocol:CONTROL_PROTOCOL,request_id:query.request_id,thread_id:threadId,cwd,
    config_digest:createHash('sha256').update(canonical(fingerprints)).digest('hex'),
    effective_servers,layers,runtime_servers};
}

// Fixed minimal identity diagnostic. Not a tool/method/argument forwarding API.
export async function inspectIdentity(request, query) {
  if(query?.operation!=='identity' || typeof query.entry_digest!=='string' ||
      !/^[a-f0-9]{64}$/.test(query.entry_digest) ||
      Object.keys(query).some(k=>!['protocol','request_id','operation','thread_id','entry_digest'].includes(k))) {
    throw Error('invalid_receiver_control');
  }
  const inspected=await configuration(request,{protocol:query.protocol,request_id:query.request_id,
    operation:'configuration',thread_id:query.thread_id},query.entry_digest);
  let result, identity;
  try {
    result=await request('mcpServer/tool/call',{threadId:query.thread_id,
      server:'agent-mailbox-project',tool:'project_identity',arguments:{}});
    if(result?.isError || !Array.isArray(result?.content) || result.content.length!==1 ||
        result.content[0]?.type!=='text' || typeof result.content[0].text!=='string' ||
        Buffer.byteLength(result.content[0].text)>8192)throw Error();
    identity=JSON.parse(result.content[0].text);
    if(identity?.protocol!=='agent-mailbox.identity.v1' ||
        Object.keys(identity).sort().join(',')!=='employee_id,project_id,protocol,session_id' ||
        ['employee_id','project_id','session_id'].some(k=>typeof identity[k]!=='string' ||
          !/^[A-Za-z0-9_-]{1,128}$/.test(identity[k])))throw Error();
  } catch {throw Error('receiver_identity_unconfirmed');}
  return {protocol:CONTROL_PROTOCOL,request_id:query.request_id,thread_id:query.thread_id,
    cwd:inspected.cwd,identity};
}

export function createReceiver({home, stateDir, candidates, admitDelivery=admit}) {
  return async envelope => {
    const receipt=deliveryReceipt(envelope);
    // Read the sender's actual outbox; a socket request cannot invent another batch.
    const outbox=JSON.parse(await fs.readFile(path.join(home,'wake','wake-outbox',receipt.delivery_id+'.json'),'utf8'));
    const identity=JSON.stringify(receipt);
    if(JSON.stringify(deliveryReceipt(outbox))!==identity) throw Error('outbox_identity_mismatch');
    const loadBindings=async()=>{
      const data=JSON.parse(await fs.readFile(path.join(home,'wake','host-bindings.json'),'utf8'));
      if(data.version!==1 || !Array.isArray(data.bindings))throw Error('invalid_bindings');
      return data.bindings;
    };
    const b=envelope.binding, fields=['id','employee_id','project_id','host_kind','host_session_id'];
    const rows=(await loadBindings()).filter(v=>v.employee_id===b.employee_id && v.project_id===b.project_id);
    if(rows.length!==1 || !fields.every(k=>rows[0][k]===b[k]))return {...receipt,status:'not_received'};
    await fs.mkdir(stateDir,{recursive:true,mode:0o700});await privatePath(stateDir,true);
    if(await fs.realpath(stateDir)!==stateDir)throw Error('canonical_state_directory_required');
    const key=createHash('sha256').update(JSON.stringify([home,receipt.delivery_id])).digest('hex');
    const file=path.join(stateDir,key+'.json');
    let claim;
    try {claim=await fs.open(file,'wx',0o600);} catch(e) {
      if(e.code!=='EEXIST')throw e;
      await privatePath(file);
      const prior=JSON.parse(await fs.readFile(file,'utf8'));
      if(prior.identity!==identity)throw Error('delivery_identity_conflict');
      if(!['received','not_received'].includes(prior.result?.status))throw Error('prior_admission_unconfirmed');
      // Rebuild only an exact receipt, never trust arbitrary persisted fields.
      return {...receipt,status:prior.result.status};
    }
    // Durable uncertainty before any host call. A crash or lost reply cannot replay it.
    try {await claim.writeFile(JSON.stringify({identity,result:{status:'unknown'}}));await claim.sync();}
    finally {await claim.close();}
    await syncDirectory(stateDir);
    const result=await admitDelivery(envelope,{loadBindings,candidates});
    const temporary=path.join(stateDir,'.'+randomUUID());
    try {
      const f=await fs.open(temporary,'wx',0o600);
      try {await f.writeFile(JSON.stringify({identity,result:{status:result.status}}));await f.sync();}
      finally {await f.close();}
      await fs.rename(temporary,file);
      await syncDirectory(stateDir);
    } finally {await fs.rm(temporary,{force:true});}
    // The sender may already have timed out. This allows strict late reconciliation.
    await writeReceipt(home,result);
    return result;
  };
}

export async function startNativeReceiver({home, socketPath, backendSocket, stateDir}) {
  if(!path.isAbsolute(home) || !path.isAbsolute(socketPath) || Buffer.byteLength(socketPath)>100) {
    throw Error('explicit_private_receiver_paths_required');
  }
  const parent=path.dirname(socketPath);
  await privatePath(parent,true);
  if(await fs.realpath(parent)!==parent)throw Error('canonical_endpoint_directory_required');
  // This unique launcher-owned pathname is never replaced, even if it looks stale.
  try {await fs.lstat(socketPath);throw Error('receiver_path_exists');}
  catch(e){if(e.code!=='ENOENT')throw e;}
  const backend=async()=>{
    const ws=await connect(backendSocket), request=rpc(ws);
    try {
      await request('initialize',{clientInfo:{name:'agent_mailbox_native_notification',version:'0.0.1'},capabilities:{experimentalApi:true}});
      ws.send(JSON.stringify({method:'initialized'}));
      return {request,close:()=>ws.terminate()};
    } catch(e) {ws.terminate();throw e;}
  };
  const receive=createReceiver({home,stateDir,candidates:async()=>[await backend()]});
  const control=async query=>{
    const client=await backend();
    let timer;
    try {
      const inspect=query.operation==='identity'?inspectIdentity:
        query.operation==='configuration'?inspectConfiguration:
        query.operation==='health'?inspectHealth:inspectSessions;
      return await Promise.race([inspect(client.request,query),new Promise((_,reject)=>{
        timer=setTimeout(()=>reject(Error('receiver_control_timeout')),7000);
      })]);
    } finally {clearTimeout(timer);client.close();}
  };
  const clients=new Set();
  const server=net.createServer(socket=>{
    clients.add(socket);socket.on('close',()=>clients.delete(socket));socket.on('error',()=>{});
    socket.setTimeout(10_000,()=>socket.destroy());
    let buffer=Buffer.alloc(0), submitted=false;
    socket.on('data',chunk=>{
      if(submitted)return;
      buffer=Buffer.concat([buffer,chunk]);
      if(buffer.length>65536){socket.destroy();return;}
      const end=buffer.indexOf(10);if(end<0)return;
      submitted=true;
      Promise.resolve().then(()=>{
        const value=JSON.parse(buffer.subarray(0,end).toString('utf8'));
        return value?.protocol===CONTROL_PROTOCOL?control(value):receive(value);
      }).then(
        receipt=>socket.end(JSON.stringify(receipt)+'\n'),
        ()=>socket.end('{"error":"delivery_unconfirmed"}\n'),
      );
    });
  });
  try {
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(socketPath,resolve);});
    await fs.chmod(socketPath,0o600);
  } catch(e) {server.close();throw e;}
  const owned=await fs.lstat(socketPath);
  let stopping;
  return ()=>stopping??=(async()=>{
    for(const socket of clients)socket.destroy();
    await new Promise(resolve=>server.close(resolve));
    const current=await fs.lstat(socketPath).catch(()=>null);
    if(current?.ino===owned.ino && current?.dev===owned.dev)await fs.unlink(socketPath);
  })();
}
