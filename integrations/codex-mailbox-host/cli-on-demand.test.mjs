import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import {spawn,spawnSync,execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {startCliReceiver} from './cli-on-demand.mjs';

test('receiver accepts an owner Codex home with the normal 0755 directory mode',async()=>{
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'am-cli-home-')));
  const home=path.join(root,'mail'),base=path.join(root,'ipc'),codexHome=path.join(root,'codex');
  await Promise.all([fs.mkdir(home,{mode:0o700}),fs.mkdir(base,{mode:0o700}),
    fs.mkdir(codexHome,{mode:0o755})]);
  const binary=path.join(root,'synthetic-binary');
  await fs.writeFile(binary,'#!/bin/sh\nexit 0\n',{mode:0o700});
  let stop;
  try{
    stop=await startCliReceiver({home,socketPath:path.join(base,'receiver.sock'),
      stateDir:path.join(base,'deliveries'),config:{binary,codex_home:codexHome,cwd:root,
        binding_id:'binding_cli',employee_id:'employee_cli',project_id:'project_cli',thread_id:'thread_cli'}});
    assert.equal(typeof stop,'function');
  }finally{if(stop)await stop();await fs.rm(root,{recursive:true,force:true});}
});

test('one CLI employee admits only one active turn across two authorized projects',{timeout:12000},async()=>{
  const root=await fs.realpath(await fs.mkdtemp('/private/tmp/am-cli-one-'));
  const home=path.join(root,'mail'),base=path.join(root,'ipc'),codexHome=path.join(root,'codex');
  await Promise.all([fs.mkdir(home,{mode:0o700}),fs.mkdir(base,{mode:0o700}),
    fs.mkdir(codexHome,{mode:0o700})]);
  const wake=path.join(home,'wake'),outbox=path.join(wake,'wake-outbox');
  await fs.mkdir(outbox,{recursive:true,mode:0o700});
  const binary=path.join(root,'synthetic-cli');
  await fs.writeFile(binary,'#!/usr/bin/env node\n'+
    'process.stdout.write(JSON.stringify({type:"thread.started",thread_id:"one_thread"})+"\\n");'+
    'setTimeout(()=>process.exit(0),4000);\n',{mode:0o700});
  const bindings=['project_a','project_b'].map((project_id,index)=>({
    id:`binding_${index}`,employee_id:'employee_one',project_id,host_kind:'codex',host_session_id:'one_thread',
  }));
  await fs.writeFile(path.join(wake,'host-bindings.json'),JSON.stringify({version:1,bindings}));
  const stateDir=path.join(base,'employee_one','state');
  const sockets=bindings.map((_,index)=>path.join(base,`receiver_${index}.sock`));
  const stops=[];
  const send=async(index)=>{
    const binding=bindings[index],delivery_id=`wake-shared-${index}`;
    const envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id,
      employee_id:binding.employee_id,binding,fresh:[`message_${index}`]};
    await fs.writeFile(path.join(outbox,delivery_id+'.json'),JSON.stringify(envelope));
    return new Promise((resolve,reject)=>{
      const socket=net.createConnection(sockets[index]);
      let answer='';
      socket.setTimeout(8000,()=>socket.destroy(Error('synthetic_delivery_timeout')));
      socket.on('connect',()=>socket.write(JSON.stringify(envelope)+'\n'));
      socket.on('data',chunk=>answer+=chunk);
      socket.on('error',reject);
      socket.on('end',()=>{try{resolve(JSON.parse(answer));}catch(error){reject(error);}});
    });
  };
  try{
    for(let index=0;index<2;index++)stops.push(await startCliReceiver({
      home,socketPath:sockets[index],stateDir,config:{binary,codex_home:codexHome,cwd:root,
        binding_id:bindings[index].id,employee_id:'employee_one',
        project_id:bindings[index].project_id,thread_id:'one_thread'},
    }));
    assert.equal((await send(0)).status,'received');
    assert.equal((await send(1)).status,'not_received',
      'the second project must not concurrently resume the same employee');
    assert.ok((await fs.stat(path.join(stateDir,'cli-active.lock'))).isFile());
  }finally{
    await Promise.all(stops.map(stop=>stop()));
    await fs.rm(root,{recursive:true,force:true});
  }
});

test('a committed mailbox delivery resumes its stopped Codex CLI thread',
  {skip:!process.env.TEST_CODEX_BINARY||!process.env.TEST_MAILBOX_PYTHON,timeout:45000},async()=>{
    const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'am-cli-mail-')));
    const home=path.join(root,'mail'),base=path.join(root,'ipc');
    await fs.mkdir(home,{mode:0o700});await fs.mkdir(base,{mode:0o700});
    assert.equal(spawnSync('/usr/bin/git',['init','-q',root]).status,0);
    const requests=[];
    let releaseSecond,releaseThird;
    const secondCanFinish=new Promise(resolve=>{releaseSecond=resolve;});
    const thirdCanFinish=new Promise(resolve=>{releaseThird=resolve;});
    const server=http.createServer(async(req,res)=>{
      let raw='';for await(const chunk of req)raw+=chunk;
      requests.push(JSON.parse(raw));const n=requests.length;
      const item={type:'message',id:`msg_${n}`,role:'assistant',status:'completed',
        content:[{type:'output_text',text:`SYNTHETIC_COMPLETE_${n}`,annotations:[]}]};
      const send=e=>res.write(`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`);
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      send({type:'response.created',response:{id:`resp_${n}`,status:'in_progress',output:[]}});
      if(n===2)await secondCanFinish;
      if(n===3)await thirdCanFinish;
      send({type:'response.output_item.added',output_index:0,item:{...item,status:'in_progress',content:[]}});
      send({type:'response.output_text.delta',output_index:0,item_id:item.id,content_index:0,delta:item.content[0].text});
      send({type:'response.output_item.done',output_index:0,item});
      send({type:'response.completed',response:{id:`resp_${n}`,status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}}});
      res.end();
    });
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    let stop;
    try{
      await fs.writeFile(path.join(root,'config.toml'),`model_provider="fixture"\nmodel="fixture"\n[model_providers.fixture]\nname="Local fixed response"\nbase_url="http://127.0.0.1:${server.address().port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n`);
      const binary=process.env.TEST_CODEX_BINARY;
      const run=args=>new Promise((resolve,reject)=>{
        const child=spawn(binary,args,{cwd:root,env:{PATH:process.env.PATH,HOME:root,CODEX_HOME:root,TMPDIR:root},stdio:['ignore','pipe','pipe']});
        let out='',err='';child.stdout.on('data',d=>out+=d);child.stderr.on('data',d=>err+=d);
        child.once('error',reject);child.once('exit',code=>resolve({code,out,err}));
      });
      const first=await run(['exec','--json','--skip-git-repo-check','SYNTHETIC_FIRST']);
      assert.equal(first.code,0,first.err.slice(-300));
      const thread=first.out.split('\n').filter(Boolean).map(JSON.parse).find(e=>e.type==='thread.started')?.thread_id;
      assert.ok(thread);
      const binding={id:'binding_cli',employee_id:'employee_cli',project_id:'project_cli',host_kind:'codex',host_session_id:thread};
      const wake=path.join(home,'wake'),outbox=path.join(wake,'wake-outbox');
      await fs.mkdir(outbox,{recursive:true,mode:0o700});
      await fs.writeFile(path.join(wake,'host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
      const deliveryId='wake-cli-on-demand',envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',
        delivery_id:deliveryId,employee_id:binding.employee_id,binding,fresh:['message_synthetic']};
      await fs.writeFile(path.join(outbox,deliveryId+'.json'),JSON.stringify(envelope));
      const endpoint=path.join(base,'receiver.sock');
      stop=await startCliReceiver({home,socketPath:endpoint,stateDir:path.join(base,'deliveries'),
        config:{binary,codex_home:root,cwd:root,binding_id:binding.id,employee_id:binding.employee_id,
          project_id:binding.project_id,thread_id:thread}});
      const py='import json,sys\nfrom agent_mailbox.workbench_host_transport import deliver\nfrom pathlib import Path\nh=Path(sys.argv[1]);s=Path(sys.argv[2]);b=json.loads((h/"wake/wake-outbox"/(sys.argv[3]+".json")).read_text());deliver(h,s,b);print((h/"wake/receipts"/sys.argv[3]).read_text())';
      const sendDelivery=async id=>{
        const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,
          ['-c',py,home,endpoint,id],{env:{PATH:process.env.PATH,PYTHONPATH:path.resolve('src')},timeout:15000});
        return JSON.parse(stdout);
      };
      const stageDelivery=async(id,message)=>{
        await fs.writeFile(path.join(outbox,id+'.json'),JSON.stringify({
          ...envelope,delivery_id:id,fresh:[message],
        }));
      };
      const health=async()=>{
        const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,
          ['-c','import sys;from agent_mailbox.workbench_host_transport import _codex_cli_health;print(_codex_cli_health(sys.argv[1],sys.argv[2]))',endpoint,thread],
          {env:{PATH:process.env.PATH,PYTHONPATH:path.resolve('src')},timeout:5000});
        return stdout.trim()==='True';
      };
      assert.equal(await health(),true);
      const result=await sendDelivery(deliveryId);
      assert.equal(result.status,'received');
      assert.equal(result.host_session_id,thread);
      const deadline=Date.now()+5000;
      while(requests.length<2 && Date.now()<deadline)await new Promise(r=>setTimeout(r,20));
      assert.equal(requests.length,2);
      const input=JSON.stringify(requests[1].input);
      assert.match(input,/SYNTHETIC_COMPLETE_1/);
      assert.match(input,/wake-cli-on-demand/);
      assert.match(input,/message_synthetic/);
      assert.equal(await health(),false,'the active CLI must not admit another turn');
      assert.equal((await sendDelivery(deliveryId)).status,'received','duplicate delivery reuses its receipt');
      await stageDelivery('wake-cli-busy','message_busy');
      assert.equal((await sendDelivery('wake-cli-busy')).status,'not_received');
      assert.equal(requests.length,2,'busy delivery must not start a second CLI');
      await fs.writeFile(path.join(wake,'host-bindings.json'),JSON.stringify({version:1,bindings:[]}));
      await stageDelivery('wake-cli-revoked','message_revoked');
      assert.equal((await sendDelivery('wake-cli-revoked')).status,'not_received');
      assert.equal(await health(),false,'revoked route must not advertise readiness');
      releaseSecond();
      const lockPath=path.join(base,'deliveries','cli-active.lock');
      const completedBy=Date.now()+5000;
      while(await fs.stat(lockPath).then(()=>true,()=>false) && Date.now()<completedBy){
        await new Promise(r=>setTimeout(r,25));
      }
      assert.equal(await fs.stat(lockPath).then(()=>true,()=>false),false,
        'normal CLI completion must release its active lock');
      const receipt=JSON.parse(await fs.readFile(path.join(wake,'receipts',deliveryId),'utf8'));
      assert.equal(receipt.status,'received');
      assert.equal(receipt.binding_id,binding.id);
      await fs.writeFile(path.join(wake,'host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
      assert.equal(await health(),true);
      await stageDelivery('wake-cli-stop-unknown','message_stop_unknown');
      assert.equal((await sendDelivery('wake-cli-stop-unknown')).status,'received');
      const thirdBy=Date.now()+5000;
      while(requests.length<3 && Date.now()<thirdBy)await new Promise(r=>setTimeout(r,20));
      assert.equal(requests.length,3);
      await stop();
      stop=await startCliReceiver({home,socketPath:endpoint,stateDir:path.join(base,'deliveries'),
        config:{binary,codex_home:root,cwd:root,binding_id:binding.id,employee_id:binding.employee_id,
          project_id:binding.project_id,thread_id:thread}});
      assert.equal(await health(),false,'stopped active execution must remain unavailable');
      assert.ok((await fs.stat(lockPath)).isFile(),'stop leaves the uncertain execution lock');
    }finally{
      releaseSecond();
      releaseThird();
      if(stop)await stop();
      server.closeAllConnections();server.close();
      await fs.rm(root,{recursive:true,force:true});
    }
  });

test('a killed CLI receiver restarts but preserves unknown execution as unavailable',
  {skip:!process.env.TEST_MAILBOX_PYTHON,timeout:20000},async()=>{
    const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'am-cli-crash-')));
    const home=path.join(root,'mail'),base=path.join(root,'ipc'),codexHome=path.join(root,'codex');
    await Promise.all([fs.mkdir(home,{mode:0o700}),fs.mkdir(base,{mode:0o700}),
      fs.mkdir(codexHome,{mode:0o700})]);
    const binding={id:'binding_crash',employee_id:'employee_crash',project_id:'project_crash',
      host_kind:'codex',host_session_id:'thread_crash'};
    const wake=path.join(home,'wake'),outbox=path.join(wake,'wake-outbox');
    await fs.mkdir(outbox,{recursive:true,mode:0o700});
    await fs.writeFile(path.join(wake,'host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
    const binary=path.join(root,'fixed-host');
    await fs.writeFile(binary,'#!/usr/bin/env node\n'+
      'process.stdout.write(JSON.stringify({type:"thread.started",thread_id:"thread_crash"})+"\\n");'+
      'setTimeout(()=>process.exit(0),2500);\n',{mode:0o700});
    const config={binary,codex_home:codexHome,cwd:root,binding_id:binding.id,
      employee_id:binding.employee_id,project_id:binding.project_id,thread_id:binding.host_session_id};
    const configFile=path.join(root,'config.json');
    await fs.writeFile(configFile,JSON.stringify(config),{mode:0o600});
    const endpoint=path.join(base,'receiver.sock'),stateDir=path.join(base,'deliveries');
    const receiver=spawn(process.execPath,[new URL('./cli-on-demand.mjs',import.meta.url).pathname,
      home,endpoint,stateDir,configFile],{stdio:['ignore','ignore','pipe']});
    let receiverError='',stop;
    receiver.stderr.on('data',chunk=>receiverError+=chunk);
    const health=async()=>{
      const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,
        ['-c','import sys;from agent_mailbox.workbench_host_transport import _codex_cli_health;print(_codex_cli_health(sys.argv[1],sys.argv[2]))',endpoint,binding.host_session_id],
        {env:{PATH:process.env.PATH,PYTHONPATH:path.resolve('src')},timeout:3000});
      return stdout.trim()==='True';
    };
    const send=async id=>{
      await fs.writeFile(path.join(outbox,id+'.json'),JSON.stringify({
        receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:id,
        employee_id:binding.employee_id,binding,fresh:['message_'+id],
      }));
      const py='import json,sys\nfrom agent_mailbox.workbench_host_transport import deliver\nfrom pathlib import Path\nh=Path(sys.argv[1]);s=Path(sys.argv[2]);b=json.loads((h/"wake/wake-outbox"/(sys.argv[3]+".json")).read_text());deliver(h,s,b);print((h/"wake/receipts"/sys.argv[3]).read_text())';
      const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,
        ['-c',py,home,endpoint,id],{env:{PATH:process.env.PATH,PYTHONPATH:path.resolve('src')},timeout:5000});
      return JSON.parse(stdout);
    };
    try {
      const deadline=Date.now()+4000;
      while(!(await health()) && Date.now()<deadline)await new Promise(r=>setTimeout(r,30));
      assert.equal(await health(),true,receiverError);
      assert.equal((await send('wake-cli-before-crash')).status,'received');
      receiver.kill('SIGKILL');
      await new Promise(resolve=>receiver.once('close',resolve));
      assert.ok((await fs.stat(path.join(stateDir,'cli-active.lock'))).isFile());
      stop=await startCliReceiver({home,socketPath:endpoint,stateDir,config});
      assert.equal(await health(),false,'orphaned run is uncertain, so restarted receiver stays closed');
      assert.equal((await send('wake-cli-after-crash')).status,'not_received');
      assert.ok((await fs.stat(path.join(stateDir,'cli-active.lock'))).isFile());
    } finally {
      if(receiver.exitCode===null && receiver.signalCode===null)receiver.kill('SIGKILL');
      if(stop)await stop();
      await fs.rm(root,{recursive:true,force:true});
    }
  });
