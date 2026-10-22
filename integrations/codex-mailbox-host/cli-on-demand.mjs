// Owner-pinned Codex CLI mailbox endpoint. The receiver is lightweight; Codex
// itself runs only for an admitted delivery, resuming the CLI's saved thread.
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import readline from 'node:readline';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {createReceiver,CONTROL_PROTOCOL} from './native.mjs';
import {deliveryReceipt,privatePath} from './client.mjs';
import {notifyHostIdle} from './launcher.mjs';

export const CLI_HEALTH_PROTOCOL='agent-mailbox.cli-receiver-health.v1';
const valid=v=>typeof v==='string' && /^[A-Za-z0-9_-]{1,128}$/.test(v);

async function removeDeadReceiverSocket(socketPath) {
  const prior=await fs.lstat(socketPath).catch(error=>{
    if(error.code==='ENOENT')return null;
    throw error;
  });
  if(!prior)return;
  if(!prior.isSocket() || prior.uid!==process.getuid() || prior.mode&0o077) {
    throw Error('owner_private_receiver_socket_required');
  }
  const unavailable=await new Promise((resolve,reject)=>{
    const probe=net.createConnection(socketPath);
    const timeout=setTimeout(()=>{probe.destroy();reject(Error('receiver_socket_state_unconfirmed'));},500);
    probe.once('connect',()=>{clearTimeout(timeout);probe.destroy();resolve(false);});
    probe.once('error',error=>{
      clearTimeout(timeout);
      if(error.code==='ECONNREFUSED')resolve(true);
      else reject(error);
    });
  });
  if(!unavailable)throw Error('receiver_already_running');
  const current=await fs.lstat(socketPath);
  if(current.ino!==prior.ino || current.dev!==prior.dev)throw Error('receiver_socket_changed');
  await fs.unlink(socketPath);
}

export function cliNotification(envelope) {
  const b=envelope.binding;
  return 'Automated agent-mailbox Host notification, not a Human message or new authorization. '+
    'Use your own project_context first and verify recipient and project. '+
    'If project_message_open is available, explicitly open each indicated message with it; '+
    'opening records first-open evidence, not task acceptance. '+
    'Read pinned resources before accepted task work; reply/submit only through agent-mailbox. '+
    JSON.stringify({project_id:b.project_id,employee_id:b.employee_id,
      message_ids:envelope.fresh,delivery_id:envelope.delivery_id});
}

export async function startCliReceiver({home,socketPath,stateDir,config}) {
  if(!path.isAbsolute(home)||!path.isAbsolute(socketPath)||Buffer.byteLength(socketPath)>100 ||
     !path.isAbsolute(stateDir) || !config ||
     !['binary','codex_home','cwd'].every(k=>typeof config[k]==='string' && path.isAbsolute(config[k])) ||
     !['binding_id','employee_id','project_id','thread_id'].every(k=>valid(config[k]))) {
    throw Error('invalid_cli_endpoint_config');
  }
  await privatePath(path.dirname(socketPath),true);
  // Codex's ordinary user home is commonly 0755. It is not our private
  // receiver state; require owner control and no group/other write instead.
  const codexHomeStat=await fs.lstat(config.codex_home);
  if(!codexHomeStat.isDirectory() || codexHomeStat.uid!==process.getuid() ||
     codexHomeStat.mode&0o022 || await fs.realpath(config.codex_home)!==config.codex_home) {
    throw Error('owner_codex_home_required');
  }
  await fs.mkdir(stateDir,{recursive:true,mode:0o700});
  await privatePath(stateDir,true);
  const binary=await fs.realpath(config.binary),cwd=await fs.realpath(config.cwd);
  const binaryStat=await fs.stat(binary),cwdStat=await fs.stat(cwd);
  if(!binaryStat.isFile() || !(binaryStat.mode&0o111) || !cwdStat.isDirectory())throw Error('invalid_cli_binary_or_cwd');
  if(await fs.realpath(home)!==home || await fs.realpath(path.dirname(socketPath))!==path.dirname(socketPath)) {
    throw Error('canonical_private_paths_required');
  }
  await removeDeadReceiverSocket(socketPath);
  const active=new Map(),sockets=new Set();
  let stopping=false;
  const activeLock=path.join(stateDir,'cli-active.lock');
  const matching=async b=>{
    if(b.id!==config.binding_id||b.employee_id!==config.employee_id||
       b.project_id!==config.project_id||b.host_session_id!==config.thread_id||b.host_kind!=='codex')return false;
    const data=JSON.parse(await fs.readFile(path.join(home,'wake','host-bindings.json'),'utf8'));
    return data.version===1 && Array.isArray(data.bindings) &&
      data.bindings.filter(row=>row.employee_id===b.employee_id && row.project_id===b.project_id).length===1 &&
      data.bindings.some(row=>['id','employee_id','project_id','host_kind','host_session_id']
        .every(k=>row[k]===b[k]));
  };
  const admitDelivery=async(envelope,{loadBindings})=>{
    const receipt=deliveryReceipt(envelope),b=envelope.binding;
    if(!await matching(b) || active.has(b.host_session_id))return {...receipt,status:'not_received'};
    // Recheck after the durable unknown journal and immediately before spawn.
    if(!(await loadBindings()).some(row=>['id','employee_id','project_id','host_kind','host_session_id']
      .every(k=>row[k]===b[k])))return {...receipt,status:'not_received'};
    let lock;
    try {lock=await fs.open(activeLock,'wx',0o600);} catch(error) {
      if(error.code==='EEXIST')return {...receipt,status:'not_received'};
      throw error;
    }
    try {await lock.writeFile(JSON.stringify({binding_id:b.id,delivery_id:receipt.delivery_id}));await lock.sync();}
    finally {await lock.close();}
    const dir=await fs.open(stateDir,'r');
    try{await dir.sync();}finally{await dir.close();}
    let child;
    try {
      child=spawn(binary,['exec','resume','--json',b.host_session_id,cliNotification(envelope)],{
        cwd,env:{...process.env,CODEX_HOME:config.codex_home},stdio:['ignore','pipe','pipe'],
      });
    } catch(error) {await fs.rm(activeLock,{force:true});throw error;}
    const completed=new Promise(resolve=>child.once('close',code=>{
      active.delete(b.host_session_id);
      // Only a clean, unforced completion releases admission. SIGTERM during
      // receiver shutdown or a failed CLI can leave effects in child processes.
      // Keep the durable lock until an owner reviews the unknown delivery.
      if(stopping || code!==0){resolve();return;}
      void fs.rm(activeLock,{force:true})
        .then(()=>notifyHostIdle(path.dirname(socketPath),b.host_session_id))
        .catch(()=>false).then(resolve);
    }));
    active.set(b.host_session_id,{child,completed});
    child.stderr.resume(); // Never persist model output or account errors.
    let settled=false,timer;
    const admitted=await new Promise((resolve,reject)=>{
      const finish=(error,ok=false)=>{
        if(settled)return;settled=true;clearTimeout(timer);
        error?reject(error):resolve(ok);
      };
      const lines=readline.createInterface({input:child.stdout});
      lines.on('line',line=>{
        let event;try{event=JSON.parse(line);}catch{return;}
        if(event.type==='thread.started'){
          if(event.thread_id!==b.host_session_id){child.kill('SIGTERM');finish(Error('resumed_thread_mismatch'));}
          else finish(null,true);
        }
      });
      child.once('error',error=>finish(error));
      child.once('close',()=>finish(Error('cli_exited_before_admission')));
      timer=setTimeout(()=>finish(Error('cli_admission_unconfirmed')),6500);
    });
    return {...receipt,status:admitted?'received':'not_received'};
  };
  const receive=createReceiver({home,stateDir,candidates:async()=>[],admitDelivery});
  const server=net.createServer(socket=>{
    sockets.add(socket);socket.on('close',()=>sockets.delete(socket));socket.on('error',()=>{});
    socket.setTimeout(10_000,()=>socket.destroy());
    let buffer=Buffer.alloc(0),submitted=false;
    socket.on('data',chunk=>{
      if(submitted)return;
      buffer=Buffer.concat([buffer,chunk]);
      if(buffer.length>65536){socket.destroy();return;}
      const end=buffer.indexOf(10);if(end<0)return;
      submitted=true;
      Promise.resolve().then(async()=>{
        const value=JSON.parse(buffer.subarray(0,end).toString('utf8'));
        if(value?.protocol!==CONTROL_PROTOCOL)return receive(value);
        if(value.operation!=='health'||!valid(value.request_id)||value.thread_id!==config.thread_id||
           Object.keys(value).some(k=>!['protocol','request_id','operation','thread_id'].includes(k))) {
          throw Error('invalid_cli_health_request');
        }
        const b={id:config.binding_id,employee_id:config.employee_id,project_id:config.project_id,
          host_kind:'codex',host_session_id:config.thread_id};
        return {protocol:CLI_HEALTH_PROTOCOL,request_id:value.request_id,thread_id:config.thread_id,
          resumable:await matching(b),admission_available:
            !active.has(config.thread_id) && !(await fs.stat(activeLock).catch(error=>{
              if(error.code==='ENOENT')return null;throw error;
            }))};
      }).then(result=>socket.end(JSON.stringify(result)+'\n'),
        ()=>socket.end('{"error":"delivery_unconfirmed"}\n'));
    });
  });
  try {
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(socketPath,resolve);});
    await fs.chmod(socketPath,0o600);
  } catch(error){server.close();throw error;}
  const owned=await fs.lstat(socketPath);
  let stopPromise;
  return ()=>stopPromise??=(async()=>{
    stopping=true;
    for(const socket of sockets)socket.destroy();
    for(const {child} of active.values())child.kill('SIGTERM');
    await Promise.all([...active.values()].map(v=>v.completed));
    await new Promise(resolve=>server.close(resolve));
    const current=await fs.lstat(socketPath).catch(()=>null);
    if(current?.ino===owned.ino && current?.dev===owned.dev)await fs.unlink(socketPath);
  })();
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href){
  // This process owns only the CLI receiver. Bind its socket private from the
  // first instant, before an external health probe can observe it.
  process.umask(0o177);
  const [home,socketPath,stateDir,configFile]=process.argv.slice(2);
  try{
    await privatePath(configFile);
    const config=JSON.parse(await fs.readFile(configFile,'utf8'));
    const stop=await startCliReceiver({home,socketPath,stateDir,config});
    const shutdown=()=>{void stop().then(()=>{process.exitCode=0;});};
    process.once('SIGTERM',shutdown);process.once('SIGINT',shutdown);
  }catch(error){console.error('Codex CLI receiver unavailable:',error.message);process.exitCode=1;}
}
