// Actual CLI with a held local response: no account, remote provider or real work.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import readline from 'node:readline';
import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {connect} from './socket.mjs';
import {rpc} from './client.mjs';

test('native mail queues behind a busy existing thread without replay or interrupting its work',
  {skip: !process.env.TEST_CODEX_BINARY || !process.env.TEST_MAILBOX_PYTHON, timeout:60000}, async t=>{
    const root=await fs.realpath(await fs.mkdtemp('/tmp/am-busy-'));
    const base=path.join(root,'ipc'), home=path.join(root,'mail');
    await fs.mkdir(base,{mode:0o700});
    await fs.mkdir(home,{mode:0o700});
    await fs.writeFile(path.join(base,'.idle-probe'),'',{mode:0o600});
    const inputs=[], events=[], stderr=[];
    const idleSignals=[], signalStatuses=[];
    let releaseFirstIdle;
    const idleServer=net.createServer(socket=>{
      let line='';socket.on('data',part=>{
        line+=part.toString();
        const end=line.indexOf('\n');
        if(end<0)return;
        const signal=JSON.parse(line.slice(0,end));
        idleSignals.push(signal);
        void (async()=>{
          const observer=await connect(manifest.socket);
          try {
            const inspect=rpc(observer);
            await inspect('initialize',{clientInfo:{name:'mailbox_completion_probe',version:'1'},
              capabilities:{experimentalApi:true}});
            observer.send(JSON.stringify({method:'initialized'}));
            const answer=await inspect('thread/read',
              {threadId:signal.thread_id,includeTurns:false});
            signalStatuses.push(answer.thread?.status?.type??'missing');
          } finally { observer.close(); }
        })().catch(error=>signalStatuses.push(`unconfirmed:${error.message}`));
        const acknowledge=()=>socket.end(JSON.stringify({protocol:'agent-mailbox.host-activity.v1',
          thread_id:signal.thread_id,processed:true})+'\n');
        if(idleSignals.length===1)releaseFirstIdle=acknowledge;
        else acknowledge();
      });
    });
    await new Promise((resolve,reject)=>{idleServer.once('error',reject);
      idleServer.listen(path.join(base,'idle.sock'),resolve);});
    t.after(()=>idleServer.close());
    let held, fixtureError;
    function complete(res,index) {
      const item={type:'message',id:`msg_${index}`,role:'assistant',status:'completed',
        content:[{type:'output_text',text:`Synthetic completion ${index}.`,annotations:[]}]};
      const send=e=>res.write(`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`);
      send({type:'response.output_item.added',output_index:0,item:{...item,status:'in_progress',content:[]}});
      send({type:'response.output_text.delta',output_index:0,item_id:item.id,content_index:0,delta:item.content[0].text});
      send({type:'response.output_item.done',output_index:0,item});
      send({type:'response.completed',response:{id:`resp_${index}`,status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}}});
      res.end();
    }
    const server=http.createServer(async(req,res)=>{
      try {
        assert.equal(req.headers.authorization,undefined);
        let body='';for await(const part of req)body+=part;
        inputs.push(JSON.parse(body));assert(inputs.length<=2,'duplicate model request');
        res.writeHead(200,{'Content-Type':'text/event-stream'});
        res.write(`event: response.created\ndata: ${JSON.stringify({type:'response.created',response:{id:`resp_${inputs.length}`,status:'in_progress',output:[]}})}\n\n`);
        if(inputs.length===1)held=res;else complete(res,inputs.length);
      } catch(e) {fixtureError=e;res.end();}
    });
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    t.after(()=>{server.closeAllConnections();server.close();});
    await fs.writeFile(path.join(root,'config.toml'),`model_provider="fixture"\nmodel="fixture"\n[model_providers.fixture]\nname="Local fixed response"\nbase_url="http://127.0.0.1:${server.address().port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n`);
    const child=spawn(process.execPath,[new URL('./launcher.mjs',import.meta.url).pathname,'app-server'],{
      cwd:root,env:{PATH:process.env.PATH,HOME:root,CODEX_HOME:root,TMPDIR:root,
        AGENT_MAILBOX_CODEX_BINARY:process.env.TEST_CODEX_BINARY,AGENT_MAILBOX_CODEX_SOCKET_DIR:base,
        AGENT_MAILBOX_CODEX_MAILBOX_HOME:home},stdio:['pipe','pipe','pipe']});
    const exited=new Promise(resolve=>child.once('exit',resolve));
    t.after(async()=>{child.kill('SIGTERM');await exited;await fs.rm(root,{recursive:true,force:true});});
    child.stderr.on('data',d=>stderr.push(d.toString()));
    let next=0;const pending=new Map();
    child.once('exit',()=>{for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('isolated child exited'));}pending.clear();});
    readline.createInterface({input:child.stdout}).on('line',line=>{
      const m=JSON.parse(line);
      if(m.method)events.push(m);
      else if(pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}
    });
    const request=(method,params)=>new Promise((resolve,reject)=>{
      const id=++next,timer=setTimeout(()=>{pending.delete(id);reject(Error(`${method} timeout`));},15000);
      pending.set(id,{resolve,reject,timer});child.stdin.write(JSON.stringify({id,method,params})+'\n');
    });
    const wait=async predicate=>{
      const deadline=Date.now()+15000;
      while(!await predicate()){
        if(fixtureError)throw fixtureError;
        if(Date.now()>deadline)throw Error(JSON.stringify({timeout:true,events:events.map(e=>({method:e.method,turn:e.params?.turn})),requests:inputs.length,stderr:stderr.join('').slice(-1000)}));
        await new Promise(r=>setTimeout(r,20));
      }
    };
    await request('initialize',{clientInfo:{name:'mailbox_busy_fixture',version:'1'},capabilities:{experimentalApi:true}});
    child.stdin.write('{"method":"initialized"}\n');
    const {thread}=await request('thread/start',{ephemeral:false,cwd:root,approvalPolicy:'never',sandbox:'read-only'});
    const first=await request('turn/start',{threadId:thread.id,input:[{type:'text',text:'SYNTHETIC_INITIAL_WORK'}]});
    await wait(()=>held!==undefined);
    assert(!events.some(e=>e.method==='turn/completed'));
    const manifest=JSON.parse(await fs.readFile(path.join(base,(await fs.readdir(base)).find(n=>n.endsWith('.json')))));
    const binding={id:'binding_busy',employee_id:'employee_busy',project_id:'project_busy',host_kind:'codex',host_session_id:thread.id};
    await fs.mkdir(path.join(home,'wake','wake-outbox'),{recursive:true});
    await fs.writeFile(path.join(home,'wake','host-bindings.json'),JSON.stringify({version:1,bindings:[binding]}));
    const script='import json,sys\nfrom pathlib import Path\nfrom agent_mailbox.workbench_host_transport import deliver,_codex_health\nh=Path(sys.argv[1]);endpoint=Path(sys.argv[2]);thread=sys.argv[3];id=sys.argv[4]\nassert _codex_health(endpoint,thread)==(sys.argv[5]=="ready")\ne=json.loads((h/"wake/wake-outbox"/(id+".json")).read_text())\ndeliver(h,endpoint,e)\nprint((h/"wake/receipts"/id).read_text())';
    async function deliver(deliveryId,ready){
      const envelope={receipt_protocol:'agent-mailbox.host-receipt.v1',delivery_id:deliveryId,
        employee_id:binding.employee_id,binding,fresh:['message_busy']};
      await fs.writeFile(path.join(home,'wake','wake-outbox',deliveryId+'.json'),JSON.stringify(envelope));
      const {stdout}=await promisify(execFile)(process.env.TEST_MAILBOX_PYTHON,
        ['-c',script,home,manifest.notification_socket,thread.id,deliveryId,ready?'ready':'busy'],
        {env:{PATH:process.env.PATH,HOME:root,PYTHONPATH:path.resolve('src')},timeout:20000});
      return JSON.parse(stdout);
    }
    const receipt=await deliver('wake-busy-test',true);
    assert.equal(receipt.status,'received');
    assert.equal(inputs.length,1);
    assert(!events.some(e=>e.method==='turn/completed'),'busy turn must not be interrupted');
    assert.equal(idleSignals.length,0);
    complete(held,1);
    await wait(()=>events.filter(e=>e.method==='turn/completed').length===2 && inputs.length===2);
    await wait(()=>idleSignals.length>=1);
    releaseFirstIdle();
    await wait(async()=>{
      const status=JSON.parse(await fs.readFile(path.join(base,'.idle-probe-result.json')));
      return status.stage==='checked';
    });
    const traced=JSON.parse(await fs.readFile(path.join(base,'.idle-probe-result.json')));
    assert.equal(traced.stage,'checked');
    assert.equal(traced.processed,true);
    assert.deepEqual(idleSignals[0],{protocol:'agent-mailbox.host-activity.v1',
      kind:'turn_completed',thread_id:thread.id});
    const idle=await request('thread/read',{threadId:thread.id,includeTurns:false});
    assert.equal(idle.thread?.status?.type,'idle');
    assert.equal(receipt.host_session_id,thread.id);
    assert.deepEqual(await deliver('wake-busy-test',true),receipt);
    assert.ifError(fixtureError);
    const continuedInput=JSON.stringify(inputs[1].input);
    assert(continuedInput.includes('wake-busy-test'));
    assert(continuedInput.includes('message_busy'));
    assert(continuedInput.includes('Synthetic completion 1.'),'first work output must be retained');
    assert.equal([...continuedInput.matchAll(/wake-busy-test/g)].length,1);
    const completions=events.filter(e=>e.method==='turn/completed').map(e=>e.params.turn);
    assert.equal(completions.length,2);assert.equal(completions[0].id,first.turn.id);
    assert.notEqual(completions[1].id,first.turn.id);
    assert.equal(events.filter(e=>e.method==='turn/started').length,2);
    assert(completions.every(turn=>turn.status==='completed'));
    assert.equal(inputs.length,2);
    t.diagnostic(JSON.stringify({real_cli:true,local_fixed_responses:2,real_model:false,
      busy_receipt:receipt.status,notification_receipt:receipt.status,
      original_turn_completed:true,followup_after_idle:true,duplicate_did_not_replay:true}));
  });
