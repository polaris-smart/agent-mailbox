// Read mailbox-owned delivery IDs and notify only a thread already loaded by Desktop.
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { connect } from './socket.mjs';

export function rpc(ws, timeout = 5000) {
  let next = 0;
  const pending = new Map();
  const fail = () => {
    for (const waiter of pending.values()) {clearTimeout(waiter.timer);waiter.reject(new Error('host_connection_closed'));}
    pending.clear();
  };
  ws.on('close', fail); ws.on('error', fail);
  ws.on('message', raw => {
    let msg; try {msg=JSON.parse(raw);} catch {fail(); return;}
    if (msg.method) return; // Never answer approvals, tools, authentication or other server requests.
    const waiter=pending.get(msg.id); if (!waiter) return;
    pending.delete(msg.id);clearTimeout(waiter.timer);
    msg.error ? waiter.reject(new Error('host_request_rejected')) : waiter.resolve(msg.result);
  });
  return (method, params={}) => new Promise((resolve,reject) => {
    const id=++next;
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error('host_request_timeout'));},timeout);
    pending.set(id,{resolve,reject,timer});
    ws.send(JSON.stringify({id,method,params}));
  });
}

export function deliveryReceipt(envelope) {
  const b=envelope?.binding, fields=['id','employee_id','project_id','host_kind','host_session_id'];
  const valid=v=>typeof v==='string' && /^[A-Za-z0-9_-]{1,512}$/.test(v);
  if (!b || fields.some(k=>!valid(b[k])) || b.host_kind!=='codex' ||
      envelope.receipt_protocol!=='agent-mailbox.host-receipt.v1' ||
      envelope.employee_id!==b.employee_id || !valid(envelope.delivery_id) || !envelope.delivery_id.startsWith('wake-') ||
      !Array.isArray(envelope.fresh) || !envelope.fresh.length || envelope.fresh.length>200 ||
      envelope.fresh.some(m=>!valid(m)) || new Set(envelope.fresh).size!==envelope.fresh.length) {
    throw new Error('invalid_mailbox_delivery');
  }
  return {protocol:envelope.receipt_protocol,delivery_id:envelope.delivery_id,employee_id:b.employee_id,
    message_ids:envelope.fresh,binding_id:b.id,project_id:b.project_id,host_kind:b.host_kind,host_session_id:b.host_session_id};
}

// Desktop's queue accepts a notice for an active or idle loaded thread. It
// runs after the current turn without steering it. Read only status metadata.
export async function threadQueueAvailable(request, threadId) {
  const result=await request('thread/read',{threadId,includeTurns:false});
  if(result?.thread?.id!==threadId)throw new Error('thread_status_unconfirmed');
  return result.thread.ephemeral===false && ['idle','active'].includes(result.thread.status?.type);
}

export async function admit(envelope, { loadBindings, candidates }) {
  const receipt=deliveryReceipt(envelope), b=envelope.binding;
  const fields=['id','employee_id','project_id','host_kind','host_session_id'];
  const current=async()=>{
    const rows=(await loadBindings()).filter(v=>v.employee_id===b.employee_id && v.project_id===b.project_id);
    return rows.length===1 && fields.every(k=>rows[0][k]===b[k]);
  };
  if (!await current()) return {...receipt,status:'not_received'};
  const clients=await candidates();
  try {
    const owners=[];
    for (const client of clients) {
      let cursor;
      const cursors=new Set();
      for(let pageIndex=0;pageIndex<20;pageIndex++) {
        const page=await client.request('thread/loaded/list',{limit:100,...cursor?{cursor}:{}});
        if (!Array.isArray(page?.data) || page.data.length>100 ||
            page.data.some(id=>typeof id!=='string' || !/^[A-Za-z0-9_-]{1,512}$/.test(id)) ||
            new Set(page.data).size!==page.data.length ||
            (page.nextCursor!=null && (typeof page.nextCursor!=='string' ||
              !page.nextCursor || page.nextCursor.length>4096))) throw new Error('invalid_loaded_threads');
        if(page.nextCursor && cursors.has(page.nextCursor))throw new Error('repeated_loaded_cursor');
        if (page.data.includes(b.host_session_id)) {owners.push(client);break;}
        if(!page.nextCursor)break;
        if(pageIndex===19)throw new Error('loaded_thread_limit');
        cursors.add(page.nextCursor);
        cursor=page.nextCursor;
      }
    }
    if (owners.length!==1 || !await current()) return {...receipt,status:'not_received'};
    if (!await threadQueueAvailable(owners[0].request,b.host_session_id)) return {...receipt,status:'not_received'};
    const text='Automated agent-mailbox Host notification, not a Human message or new authorization. '+
      'Use your own project_context first and verify recipient and project. '+
      'If project_message_open is available, explicitly open each indicated message with it; '+
      'a successful open records first-open evidence, not task acceptance. '+
      'If that tool is unavailable, use project_messages without claiming a read receipt was recorded. '+
      'Never bypass denied access or send an extra ACK for opening. Existing task instructions and Human permissions still apply. '+
      'Read pinned resources before accepted task work; reply/submit only through agent-mailbox. '+
      JSON.stringify({project_id:b.project_id,employee_id:b.employee_id,message_ids:envelope.fresh,delivery_id:envelope.delivery_id});
    // Queueing is the only supported non-steering admission when Desktop starts
    // another turn before the completion observer can signal mailbox. The
    // receiver's durable unknown claim guards the non-idempotent queue/add RPC.
    const clientUserMessageId=createHash('sha256').update(envelope.delivery_id+b.id).digest('hex');
    const result=await owners[0].request('thread/queue/add',{
      threadId:b.host_session_id,input:[{type:'text',text}],clientUserMessageId,
    });
    if (typeof result?.queuedSubmission?.id!=='string' || !result.queuedSubmission.id ||
        result.queuedSubmission.clientUserMessageId!==clientUserMessageId ||
        result.queuedSubmission.input?.length!==1 ||
        result.queuedSubmission.input[0]?.type!=='text' ||
        result.queuedSubmission.input[0]?.text!==text) throw new Error('host_admission_unconfirmed');
    return {...receipt,status:'received'};
  } finally {for(const client of clients) client.close();}
}

export async function privatePath(p, directory=false) {
  const s=await fs.lstat(p);
  if (s.isSymbolicLink() || s.uid!==process.getuid() || (s.mode&0o077) ||
      (directory ? !s.isDirectory() : !s.isFile())) throw new Error('unsafe_host_path');
}

export async function discover(base) {
  try {await privatePath(base,true);} catch(e) {if(e.code==='ENOENT')return [];throw e;}
  const clients=[];
  try {
    for (const n of await fs.readdir(base)) {
      if (!/^\d+-[a-f0-9]{12}\.json$/.test(n)) continue;
      const file=path.join(base,n);await privatePath(file);
      const m=JSON.parse(await fs.readFile(file,'utf8'));
      const socketPath=path.join(base,n.replace(/\.json$/,'.sock'));
      if(m.version!==1 || m.socket!==socketPath)throw new Error('invalid_host_manifest');
      const stat=await fs.lstat(socketPath).catch(e=>{if(e.code==='ENOENT')return null;throw e;});
      if (!stat)continue;
      if(!stat.isSocket() || stat.uid!==process.getuid() || (stat.mode&0o077))throw new Error('unsafe_host_socket');
      let ws;
      try {ws=await connect(socketPath);} catch(e) {if(['ENOENT','ECONNREFUSED'].includes(e.code))continue;throw e;}
      const request=rpc(ws);
      clients.push({request,close:()=>ws.terminate()});
      await request('initialize',{clientInfo:{name:'agent_mailbox_notification',version:'0.0.1'},capabilities:{experimentalApi:true}});
      ws.send(JSON.stringify({method:'initialized'}));
    }
    return clients;
  } catch(e) {for(const c of clients)c.close();throw e;}
}

export async function deliver({home,base,employeeId,deliveryId}) {
  if(!path.isAbsolute(home)||!path.isAbsolute(base)||!/^wake-[A-Za-z0-9_-]+$/.test(deliveryId))throw new Error('invalid_hook_arguments');
  const e=JSON.parse(await fs.readFile(path.join(home,'wake','wake-outbox',deliveryId+'.json'),'utf8'));
  if(e.delivery_id!==deliveryId||e.employee_id!==employeeId)throw new Error('outbox_identity_mismatch');
  const receipt=await admit(e,{loadBindings:async()=>{
    const d=JSON.parse(await fs.readFile(path.join(home,'wake','host-bindings.json'),'utf8'));
    if(d.version!==1||!Array.isArray(d.bindings))throw new Error('invalid_bindings');return d.bindings;
  },candidates:()=>discover(base)});
  await writeReceipt(home,receipt);
  return receipt;
}

export async function writeReceipt(home,receipt) {
  const deliveryId=receipt.delivery_id;
  if(!/^wake-[A-Za-z0-9_-]+$/.test(deliveryId))throw new Error('invalid_delivery_id');
  const directory=path.join(home,'wake','receipts');await fs.mkdir(directory,{recursive:true,mode:0o700});await privatePath(directory,true);
  const tmp=path.join(directory,'.'+randomUUID());
  try {
    const f=await fs.open(tmp,'wx',0o600);
    try {await f.writeFile(JSON.stringify(receipt));await f.sync();} finally {await f.close();}
    await fs.rename(tmp,path.join(directory,deliveryId));
  } finally {await fs.rm(tmp,{force:true});}
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const [home,base,employeeId,deliveryId]=process.argv.slice(2);
  deliver({home,base,employeeId,deliveryId}).then(r=>{process.exitCode=r.status==='received'?0:2;},
    ()=>{console.error('Codex Host delivery unconfirmed; no receipt written.');process.exitCode=1;});
}
