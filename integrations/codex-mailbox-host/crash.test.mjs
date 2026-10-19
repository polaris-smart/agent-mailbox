// Kill only disposable receiver processes; backend admission is synthetic.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';

const childSource=`
import fs from 'node:fs/promises';
import path from 'node:path';
const {createReceiver}=await import(process.env.RECEIVER_MODULE);
const root=process.env.FIXTURE_ROOT, mode=process.env.FIXTURE_MODE;
const envelope=JSON.parse(await fs.readFile(path.join(root,'envelope.json')));
const receive=createReceiver({home:path.join(root,'home'),stateDir:path.join(root,'journal'),
  candidates:async()=>[{close(){},async request(method,params){
    if(method==='thread/loaded/list')return {data:[envelope.binding.host_session_id]};
    if(method==='thread/read')return {thread:{id:envelope.binding.host_session_id,status:{type:'idle'}}};
    if(method!=='thread/queue/add')throw Error('unexpected operation');
    await fs.appendFile(path.join(root,'admissions'),'one\\n');
    if(mode==='crash-unknown') {
      process.send({checkpoint:'admission_started'});
      await new Promise(()=>{});
    }
    return {queuedSubmission:{id:'synthetic-queue',clientUserMessageId:params.clientUserMessageId,input:params.input}};
  }}]});
try {
  const receipt=await receive(envelope);
  process.send({checkpoint:'receipt',receipt});
} catch(e) {process.send({checkpoint:'error',error:e.message});}
if(mode.startsWith('crash-'))await new Promise(()=>{});
else process.disconnect();
`;

for(const outcome of ['unknown','received'])test(`SIGKILL then restart preserves ${outcome} admission without replay`,
  {timeout:15000},async t=>{
    const root=await fs.realpath(await fs.mkdtemp('/tmp/am-crash-'));
    t.after(()=>fs.rm(root,{recursive:true,force:true}));
    const home=path.join(root,'home'), stateDir=path.join(root,'journal');
    const b={id:'binding_crash',employee_id:'employee_crash',project_id:'project_crash',host_kind:'codex',host_session_id:'synthetic-thread'};
    const envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:'wake-crash-test',employee_id:b.employee_id,binding:b,fresh:['message_crash']};
    await fs.mkdir(path.join(home,'wake','wake-outbox'),{recursive:true});
    await fs.writeFile(path.join(home,'wake','host-bindings.json'),JSON.stringify({version:1,bindings:[b]}));
    await fs.writeFile(path.join(home,'wake','wake-outbox','wake-crash-test.json'),JSON.stringify(envelope));
    await fs.writeFile(path.join(root,'envelope.json'),JSON.stringify(envelope));
    function start(mode){
      const child=spawn(process.execPath,['--input-type=module','-e',childSource],{
        env:{PATH:process.env.PATH,HOME:root,FIXTURE_ROOT:root,FIXTURE_MODE:mode,
          RECEIVER_MODULE:new URL('./native.mjs',import.meta.url).href},
        stdio:['ignore','ignore','pipe','ipc']});
      let stderr='';child.stderr.on('data',d=>{stderr+=d;});
      const exited=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
      const checkpoint=new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>reject(Error('checkpoint timeout: '+stderr)),7000);
        child.once('message',v=>{clearTimeout(timer);resolve(v);});
        child.once('exit',()=>{clearTimeout(timer);reject(Error('child exited before checkpoint: '+stderr));});
      });
      t.after(async()=>{child.kill('SIGKILL');await exited;});
      return {child,exited,checkpoint};
    }
    const first=start('crash-'+outcome);
    const before=await first.checkpoint;
    assert.equal(before.checkpoint,outcome==='unknown'?'admission_started':'receipt');
    if(outcome==='received')assert.equal(before.receipt.status,'received');
    first.child.kill('SIGKILL');assert.equal((await first.exited).signal,'SIGKILL');
    const key=createHash('sha256').update(JSON.stringify([home,envelope.delivery_id])).digest('hex');
    const record=JSON.parse(await fs.readFile(path.join(stateDir,key+'.json')));
    assert.equal(record.result.status,outcome);
    const second=start('recover'), after=await second.checkpoint;
    assert.equal((await second.exited).code,0);
    if(outcome==='unknown'){
      assert.equal(after.checkpoint,'error');assert.equal(after.error,'prior_admission_unconfirmed');
      await assert.rejects(fs.stat(path.join(home,'wake','receipts',envelope.delivery_id)),/ENOENT/);
    } else {
      assert.deepEqual(after.receipt,before.receipt);
      assert.deepEqual(JSON.parse(await fs.readFile(path.join(home,'wake','receipts',envelope.delivery_id))),before.receipt);
    }
    assert.equal(await fs.readFile(path.join(root,'admissions'),'utf8'),'one\n');
  });
