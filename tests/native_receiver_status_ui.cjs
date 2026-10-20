require('./browser_sandbox_guard.cjs');
// Existing UI assets, intercepted owner GET only. No real receiver or credentials.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assets=path.resolve(process.env.MAILBOX_UI_ASSETS||path.join(__dirname,'../src/agent_mailbox/workbench_assets'));
(async()=>{
  const browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
  const results=[];
  try {
    for(const kind of ['hermes','deepseek'])for(const locale of ['zh-CN','en-US'])for(const scenario of ['states','held-status','project-race','closed-form',...(kind==='hermes'?['project-link','project-link-race','held-mail','profiles','profile-race','profile-close','diagnostic','diagnostic-race','admission','admission-existing','admission-other-route','admission-unknown','admission-race','grant','grant-offline','grant-race','grant-close','grant-invalid']:['dsh-profiles'])]){
      const context=await browser.newContext({locale,viewport:{width:locale==='zh-CN'?1200:900,height:locale==='zh-CN'?800:600}});
      const page=await context.newPage();page.setDefaultTimeout(6000);
      const fixture={projects:['A','B'].map(id=>({id,name:'Synthetic '+id,path:'/synthetic/'+id})),
        employees:[{id:'worker',name:'Synthetic receiver',kind,node_id:'local',lifecycle:'active',project_id:'A',project_ids:['A','B'],project_roles:{}}],
        node:{id:'local',name:'Synthetic device'},devices:[],tasks:[],messages:[],resources:[],memories:[],runtime:{installed:false},version:'0.8.3-test',bootstrap_version:1};
      const zh=locale==='zh-CN', requests=[], errors=[];
      let next=(scenario==='admission'||scenario==='admission-other-route'||scenario==='admission-unknown'||scenario==='admission-race')?'binding_required':'component_available',release=null,hold=false;
      let hermesRoutePresent=scenario==='admission'||scenario==='admission-race'?false:scenario==='admission-unknown'?null:true;
      const profileRequests=[];
      const candidate={id:'hermes_profile_'+'a'.repeat(64),label:'research',candidate_only:true};
      let profileResult={host_kind:'hermes',discovery_scope:'local_default_installation',state:'profile_candidates',profiles:[candidate],session_verified:false,delivery_verified:false};
      const dshCandidate={id:'dsh_profile_'+'b'.repeat(64),label:'desktop',candidate_only:true};
      let dshResult={host_kind:'deepseek',discovery_scope:'local_default_installation',state:'profile_candidates',profiles:[dshCandidate],session_verified:false,delivery_verified:false};
      let profileRelease=null,profileHold=false,profileFailure=false;
      const diagnosticCalls=[];let diagnosticState='not_prepared_here',diagnosticHold=false,diagnosticRelease=null;
      const admissionCalls=[];let admissionState='not_admitted_here',admissionPhase='first_contact_not_started';
      let grantRelease=null, grantHold=false, grantInvalid=false; const grantCalls=[];
      let linkRelease=null,linkHold=false;const linkCalls=[];
      const heldCalls=[],heldId='message_'+'a'.repeat(32);
      page.on('pageerror',e=>errors.push(e.message));
      if(scenario.startsWith('project-link')||scenario==='held-mail')page.on('dialog',dialog=>dialog.accept());
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(url.host!=='mailbox.test')return route.abort();
        if(url.pathname==='/api/workbench/bootstrap')return route.fulfill({json:fixture});
        if(url.pathname==='/api/workbench/mailbox/receivers/hermes/profiles'){
          assert.equal(req.method(),'GET');assert.equal(url.searchParams.get('employee_id'),'worker');
          assert.deepEqual([...url.searchParams.keys()].sort(),['employee_id','project_id']);
          profileRequests.push(url.searchParams.get('project_id'));
          const result=structuredClone(profileResult),failure=profileFailure;
          if(profileHold){profileHold=false;await new Promise(r=>{profileRelease=r;});}
          return route.fulfill({status:failure?500:200,json:failure?{error:{message:'/private/secret/never-display'}}:result});
        }
        if(url.pathname==='/api/workbench/mailbox/receivers/dsh/profiles'){
          assert.equal(kind,'deepseek');assert.equal(req.method(),'GET');
          assert.equal(url.searchParams.get('employee_id'),'worker');
          assert.deepEqual([...url.searchParams.keys()].sort(),['employee_id','project_id']);
          profileRequests.push(url.searchParams.get('project_id'));
          return route.fulfill({json:structuredClone(dshResult)});
        }
        if(url.pathname==='/api/workbench/mailbox/installers/hermes/diagnostic'){
          assert.equal(req.method(),'POST');
          const body=req.postDataJSON();diagnosticCalls.push(body);
          assert.equal(body.employee_id,'worker');assert.equal(body.project_id,'A');
          if(body.action==='preview')diagnosticState='preview';
          if(body.action==='apply'){
            assert.equal(body.confirmed,true);assert.equal(body.plan_id,'b'.repeat(32));
            diagnosticState='prepared_not_activated';
          }
          if(body.action==='remove'){
            assert.equal(body.confirmed,true);diagnosticState='preparation_removed';
          }
          assert(['lookup','preview','apply','status','remove'].includes(body.action));
          if(diagnosticHold && body.action==='preview'){
            diagnosticHold=false;await new Promise(r=>{diagnosticRelease=r;});
          }
          assert.deepEqual(Object.keys(body).sort(),(body.action==='lookup'||body.action==='preview'
            ? ['action','employee_id','profile_id','project_id']
            : body.action==='apply'||body.action==='remove'
              ? ['action','confirmed','employee_id','plan_id','project_id']
              : ['action','employee_id','plan_id','project_id']).sort());
          return route.fulfill({json:{profile_id:candidate.id,state:diagnosticState,
            ...(diagnosticState==='not_prepared_here'?{}:{plan_id:'b'.repeat(32)}),
            requires_confirmation:diagnosticState==='preview',will_activate:false,
            will_write_injection_grant:false,session_verified:false,
            delivery_verified:false,activation_requested:false}});
        }
        if(url.pathname==='/api/workbench/mailbox/installers/hermes/admission'){
          assert.equal(req.method(),'POST');const body=req.postDataJSON();admissionCalls.push(body);
          assert.equal(body.employee_id,'worker');assert.equal(body.project_id,'A');
          if(body.action==='lookup'){
            assert.equal(body.profile_id,candidate.id);
            return route.fulfill({json:{profile_id:candidate.id,state:admissionState,
              ...(admissionState==='not_admitted_here'?{}:{plan_id:'c'.repeat(32)}),
              runtime_verified:false,session_verified:false,delivery_verified:false}});
          }
          if(body.action==='preview'){
            assert.equal(body.preparation_id,'b'.repeat(32));admissionState='preview';
            return route.fulfill({json:{profile_id:candidate.id,plan_id:'c'.repeat(32),state:'preview',
              runtime_verified:false,session_verified:false,delivery_verified:false}});
          }
          assert.equal(body.plan_id,'c'.repeat(32));
          if(body.action==='first_contact_status')return route.fulfill({json:{state:admissionPhase,
            runtime_verified:false,session_verified:false,delivery_verified:false}});
          if(body.action==='first_contact_execute'){
            assert.equal(body.confirmed,true);admissionState='diagnostic_enabled_restart_required';
            admissionPhase='recorded_complete_runtime_unconfirmed';
            return route.fulfill({json:{state:'diagnostic_runtime_observed',runtime_verified:true,
              session_verified:false,delivery_verified:false}});
          }
          if(body.action==='verify')return route.fulfill({json:{state:'diagnostic_runtime_observed',
            runtime_verified:true,session_verified:false,delivery_verified:false}});
          throw new Error('unexpected admission action '+body.action);
        }
        if(url.pathname==='/api/workbench/mailbox/receivers/hermes/grant'){
          assert.equal(req.method(),'POST'); const body=req.postDataJSON(); grantCalls.push(body);
          assert.equal(body.employee_id,'worker'); assert.equal(body.project_id,'A'); assert.equal(body.profile_id,candidate.id);
          const applying=body.action==='apply'; assert(['preview','apply'].includes(body.action));
          assert.deepEqual(Object.keys(body).sort(),['employee_id','project_id','profile_id','action',...(applying?['confirmed','expected_binding_id','expected_config_sha256']:[])].sort());
          if(applying){assert.equal(body.confirmed,true);assert.equal(body.expected_binding_id,'binding_'+'b'.repeat(32));assert.equal(body.expected_config_sha256,'c'.repeat(64));}
          if(grantHold){grantHold=false; await new Promise(r=>{grantRelease=r;});}
          return route.fulfill({json:{profile_id:candidate.id,binding_id:'binding_'+'b'.repeat(32),config_sha256:'c'.repeat(64),
            state:applying?'grant_enabled_runtime_unverified':'grant_preview',enabled:applying,runtime_verified:grantInvalid,delivery_verified:false}});
        }
        if(url.pathname==='/api/workbench/mailbox/receivers/hermes/project-link'){
          assert.equal(req.method(),'POST');const body=req.postDataJSON();linkCalls.push(body);
          assert.equal(body.employee_id,'worker');assert(['A','B'].includes(body.project_id));
          assert(!('session_key' in body)&&!('socket_path' in body));
          if(body.action==='preview'){
            assert.deepEqual(Object.keys(body).sort(),['action','employee_id','project_id']);
            if(linkHold){linkHold=false;await new Promise(r=>{linkRelease=r;});}
            return route.fulfill({json:{state:'confirmation_required',fingerprint:'d'.repeat(64),
              repair_required:false,delivery_verified:false}});
          }
          assert.equal(body.action,'apply');assert.equal(body.fingerprint,'d'.repeat(64));
          assert.equal(body.confirmed,true);
          return route.fulfill({json:{state:'linked_runtime_unverified',delivery_verified:false}});
        }
        if(url.pathname==='/api/workbench/mailbox/receivers/hermes/held-mail'){
          assert.equal(req.method(),'POST');const body=req.postDataJSON();heldCalls.push(body);
          assert.equal(body.employee_id,'worker');assert.equal(body.project_id,'A');
          if(body.action==='list')return route.fulfill({json:{state:'held_mail_list',items:[{
            id:heldId,title:'Synthetic old letter',created_at:'2026-10-19T00:00:00Z'}],
            has_more:false,delivery_verified:false}});
          assert.equal(body.message_id,heldId);
          if(body.action==='preview')return route.fulfill({json:{state:'confirmation_required',
            message_id:heldId,title:'Synthetic old letter',body:'Synthetic private body',
            fingerprint:'f'.repeat(64),delivery_verified:false}});
          assert.equal(body.action,'apply');assert.equal(body.fingerprint,'f'.repeat(64));
          assert.equal(body.confirmed,true);
          return route.fulfill({json:{state:'old_mail_pending',delivery_verified:false}});
        }
        if(url.pathname==='/api/workbench/mailbox/receivers/native'){
          assert.equal(req.method(),'GET');assert.equal(url.searchParams.get('employee_id'),'worker');
          assert.deepEqual([...url.searchParams.keys()].sort(),['employee_id','project_id']);
          requests.push(url.searchParams.get('project_id'));
          const state=next;
          if(hold){hold=false;await new Promise(r=>{release=r;});}
          return route.fulfill({json:{host_kind:kind,state,binding_configured:state!=='binding_required',
            ...(kind==='hermes'?{hermes_route_present:hermesRoutePresent}:{}),
            component_available:state==='component_available',session_verified:false,delivery_verified:false,
            pending_alert_count:scenario==='states'?2:0,
            held_alert_count:scenario==='held-status'&&url.searchParams.get('project_id')==='A'?2:0,
            unknown_delivery_alert_count:scenario==='states'?1:0,
            oldest_pending_alert_at:scenario==='states'?'2026-10-19T00:00:00Z':null,
            ...(kind==='deepseek'?{opened_alert_retractions:{pending:1,unknown:2,binding_changed:0}}:{})}});
        }
        if(url.pathname.startsWith('/api/')){assert.equal(req.method(),'GET');return route.fulfill({json:{events:[],notifications:[],capabilities:[],revision:0}});}
        const name=path.basename(url.pathname)||'index.html';
        return route.fulfill({body:fs.readFileSync(path.join(assets,name)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
      });
      await page.addInitScript(()=>{
        const original=window.fetch.bind(window);
        window.fetch=(url,opts={})=>url==='/api/workbench/changes'?Promise.resolve(new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('{"revision":0}\n'));opts.signal?.addEventListener('abort',()=>c.close(),{once:true});}}),{headers:{'Content-Type':'application/x-ndjson'}})):original(url,opts);
      });
      await page.goto('http://mailbox.test/#token=synthetic');
      await page.locator('[data-view="mailboxes"]').first().dispatchEvent('click');
      const dialog=page.getByRole('dialog'), status=dialog.locator('#native-receiver-status');
      const open=()=>page.getByRole('button',{name:zh?'接收状态':'Reception status',exact:true}).click();
      const refresh=dialog.getByRole('button',{name:zh?'刷新状态':'Refresh status',exact:true});
      await open();await status.getByText((scenario==='admission'||scenario==='admission-other-route'||scenario==='admission-unknown'||scenario==='admission-race')
        ? (zh?'尚未授权此员工的原会话。':'No existing session is authorized for this employee.')
        : (zh?'接收组件可用。':'The receiver component is available.'),{exact:false}).waitFor();
      assert.match(await dialog.textContent(),zh?/不代表原会话已收到提醒/:/does not establish notification delivery/);
      if(scenario==='held-status'){
        const backlog=dialog.locator('#native-receiver-backlog');
        await backlog.getByText(zh?'2 条旧提醒':'2 older alerts',{exact:false}).waitFor();
        await dialog.locator('#native-receiver-project').selectOption('B');
        await backlog.waitFor({state:'hidden'});
      }else if(scenario.startsWith('project-link')){
        const section=dialog.locator('.receiver-project-link');
        await section.locator('summary').click();
        const check=section.getByRole('button',{name:zh?'检查复用已有原会话':'Check existing session link',exact:true});
        const confirm=section.getByRole('button',{name:zh?'确认连接此项目':'Confirm this project link',exact:true});
        const linked=section.getByRole('status');
        if(scenario==='project-link-race'){
          linkHold=true;await check.click();
          for(let i=0;i<100&&!linkRelease;i++)await new Promise(r=>setTimeout(r,10));assert(linkRelease);
          await dialog.locator('#native-receiver-project').selectOption('B');
          const response=page.waitForResponse(r=>r.url().includes('/hermes/project-link'));
          linkRelease();await response;
          assert.equal(await confirm.isVisible(),false);assert.deepEqual(linkCalls.map(x=>x.action),['preview']);
        }else{
          await check.click();await confirm.waitFor({state:'visible'});
          assert.match(await linked.textContent(),zh?/只连接当前项目/:/only this project/);
          await confirm.click();await linked.getByText(zh?'此项目路由已配置':'This project route is configured',{exact:false}).waitFor();
          assert.equal(await confirm.isVisible(),false);
          assert.deepEqual(linkCalls.map(x=>x.action),['preview','apply']);
          assert.equal(linkCalls[0].project_id,'A');assert.equal(linkCalls[1].project_id,'A');
        }
      }else if(scenario==='held-mail'){
        const section=dialog.locator('.receiver-held-mail');
        await section.locator('summary').click();
        await section.getByRole('button',{name:zh?'查看此项目旧提醒':'Review older alerts'}).click();
        await section.getByText('Synthetic old letter',{exact:false}).waitFor();
        await section.getByRole('button',{name:zh?'查看旧信正文':'Read older letter'}).click();
        await section.locator('.receiver-held-body').getByText('Synthetic private body',{exact:true}).waitFor();
        await section.getByRole('button',{name:zh?'确认补达这一封':'Confirm this redelivery'}).click();
        await section.getByText(zh?'这封旧信已进入待投递':'This letter is pending delivery',{exact:false}).waitFor();
        assert.deepEqual(heldCalls.map(x=>x.action),['list','preview','apply']);
        assert.equal(await section.locator('ul').isVisible(),false);
      }else if(kind==='deepseek'){
        const retraction=dialog.locator('#native-retraction-status');
        await retraction.getByText(zh?'待撤回 1':'1 awaiting retraction',{exact:false}).waitFor();
        assert.match(await retraction.textContent(),zh?/结果不确定 2/:/2 uncertain/);
      }
      if(scenario==='dsh-profiles'){
        const find=dialog.getByRole('button',{name:zh?'查找配置档':'Find profiles',exact:true});
        const ps=dialog.locator('#dsh-profile-status'),list=dialog.locator('.receiver-profile-list');
        assert.equal(profileRequests.length,0);
        await dialog.locator('.receiver-profile-discovery summary').click();
        await find.click();await list.getByText('desktop',{exact:true}).waitFor();
        assert.match(await ps.textContent(),zh?/尚未核实或连接原会话/:/unverified and unconnected/);
        assert.deepEqual(profileRequests,['A']);
        await dialog.locator('#native-receiver-project').selectOption('B');
        await ps.getByText(zh?'尚未查找':'Not checked yet',{exact:false}).waitFor();
        assert.equal(await list.isVisible(),false);
        dshResult={...dshResult,session_verified:true};
        await find.click();await ps.getByText(zh?'无法确认':'could not be confirmed',{exact:false}).waitFor();
        assert.equal(await list.isVisible(),false);
      }else if(scenario.startsWith('profile') || scenario.startsWith('diagnostic') || scenario.startsWith('admission') || scenario.startsWith('grant')){
        assert.equal(profileRequests.length,0); // Explicit discovery, not automatic scans.
        const find=dialog.getByRole('button',{name:zh?'查找配置档':'Find profiles',exact:true});
        const ps=dialog.locator('#hermes-profile-status'),list=dialog.locator('.receiver-profile-list');
        const expand=()=>dialog.locator('.receiver-profile-discovery summary').click();
        await expand();
        if(scenario.startsWith('grant')){
          await find.click();await list.getByText('research',{exact:true}).waitFor();
          const check=dialog.getByRole('button',{name:zh?'检查原会话授权':'Check session permission',exact:true});
          const confirm=dialog.getByRole('button',{name:zh?'允许原会话接收邮件提醒':'Allow mail alerts in this session',exact:true});
          const gs=dialog.locator('.hermes-grant-status');
          if(scenario==='grant-race'||scenario==='grant-close'){
            grantHold=true;await check.click();
            for(let i=0;i<100&&!grantRelease;i++)await new Promise(r=>setTimeout(r,10));assert(grantRelease);
            if(scenario==='grant-race')await dialog.locator('#native-receiver-project').selectOption('B');
            else {await dialog.getByRole('button',{name:zh?'关闭':'Close',exact:true}).click();await open();await expand();}
            const response=page.waitForResponse(r=>r.url().includes('/hermes/grant'));grantRelease();await response;
            await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
            assert.equal(await confirm.count(),0); assert.equal(grantCalls.length,1);
          }else if(scenario==='grant-invalid'){
            grantInvalid=true;await check.click();await gs.getByText(zh?'无法确认原会话授权':'Session permission could not be confirmed',{exact:false}).waitFor();
            assert.equal(await confirm.isVisible(),false);assert.equal(grantCalls.length,1);
          }else{
            await check.click();await confirm.waitFor({state:'visible'});
            assert.match(await gs.textContent(),zh?/不启动或重载/:/does not add a session or start or reload/);
            if(scenario==='grant-offline')next='receiver_offline';
            await confirm.click();await gs.getByText(zh?'原会话提醒授权已开启':'Session alert permission is enabled',{exact:false}).waitFor();
            assert.equal(await confirm.isVisible(),false);assert.deepEqual(grantCalls.map(c=>c.action),['preview','apply']);
            assert.match(await gs.textContent(),zh?/未启动或重载宿主/:/No host was started or reloaded/);
            assert.equal(requests.length,2);
            if(scenario==='grant-offline')assert.match(await status.textContent(),zh?/离线/:/offline/);
            await gs.scrollIntoViewIfNeeded();assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1));
            if(process.env.UI_EVIDENCE_DIR){fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`grant-${locale}.png`)});}
          }
        }else if(scenario==='diagnostic-race'){

          await find.click();await list.getByText('research',{exact:true}).waitFor();
          diagnosticHold=true;
          await dialog.getByRole('button',{name:zh?'预览诊断准备':'Preview diagnostic setup',exact:true}).click();
          for(let i=0;i<100&&!diagnosticRelease;i++)await new Promise(r=>setTimeout(r,10));
          assert(diagnosticRelease);
          await dialog.locator('#native-receiver-project').selectOption('B');
          await ps.getByText(zh?'尚未查找':'Not checked yet',{exact:false}).waitFor();
          const response=page.waitForResponse(r=>r.url().includes('/hermes/diagnostic'));
          diagnosticRelease();await response;
          await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
          assert.equal(await list.isVisible(),false);
          assert.deepEqual(diagnosticCalls.map(call=>call.action),['preview']);
        }else if(scenario.startsWith('admission')){
          await find.click();await list.getByText('research',{exact:true}).waitFor();
          await dialog.getByRole('button',{name:zh?'预览诊断准备':'Preview diagnostic setup',exact:true}).click();
          await dialog.getByRole('button',{name:zh?'确认准备诊断组件':'Confirm diagnostic setup',exact:true}).click();
          await dialog.getByText(zh?'诊断接入（尚不收信）':'Diagnostic admission (mail stays disconnected)',{exact:true}).click();
          await dialog.getByRole('button',{name:zh?'查看接入状态':'Check admission',exact:true}).click();
          const admitPreview=dialog.getByRole('button',{name:zh?'预览诊断接入':'Preview diagnostic admission',exact:true});
          const execute=dialog.getByRole('button',{name:zh?'确认诊断接入':'Confirm diagnostic admission',exact:true});
          if(scenario==='admission-existing'||scenario==='admission-other-route'||scenario==='admission-unknown'){
            await dialog.getByText(scenario==='admission-unknown'
              ? (zh?'无法确认此处没有既有 Hermes 路由':'Cannot confirm that no Hermes route exists here')
              : (zh?'已有 Hermes 收信路由':'An existing Hermes mail route requires a separate cutover.'),{exact:false}).waitFor();
            assert.equal(await admitPreview.isVisible(),false);
            assert.equal(await execute.isVisible(),false);
            assert.deepEqual(admissionCalls.map(call=>call.action),['lookup']);
          }else{
            await admitPreview.waitFor({state:'visible'});await admitPreview.click();
            await execute.waitFor({state:'visible'});
            page.on('dialog',prompt=>prompt.accept());
            if(scenario==='admission-race'){
              hermesRoutePresent=true;
              await execute.click();
              await dialog.getByText(zh?'已有 Hermes 收信路由':'An existing Hermes mail route requires a separate cutover.',{exact:false}).waitFor();
              assert.equal(await admitPreview.isVisible(),false);
              assert.equal(await execute.isVisible(),false);
              assert.deepEqual(admissionCalls.map(call=>call.action),[
                'lookup','preview','lookup','first_contact_status']);
            }else{
            await execute.click();
            await dialog.getByText(zh?'诊断路由已核实':'Diagnostic route verified',{exact:false}).waitFor();
            assert.equal(await execute.isVisible(),false);
            const verify=dialog.getByRole('button',{name:zh?'核验当前诊断路由':'Verify current diagnostic route',exact:true});
            await verify.waitFor({state:'visible'});await verify.click();
            await dialog.getByText(zh?'诊断路由已核实':'Diagnostic route verified',{exact:false}).waitFor();
            assert.deepEqual(admissionCalls.map(call=>call.action),['lookup','preview',
              'lookup','first_contact_status','first_contact_execute','lookup','first_contact_status',
              'verify','lookup','first_contact_status']);
            assert.match(await dialog.textContent(),zh?/原会话获知邮件、接单和交付仍未验证/:/Mail receipt in the original session, acceptance and delivery remain unverified/);
            }
          }
        }else if(scenario==='diagnostic'){
          await find.click();await list.getByText('research',{exact:true}).waitFor();
          const check=dialog.getByRole('button',{name:zh?'查看准备状态':'Check preparation',exact:true});
          const preview=dialog.getByRole('button',{name:zh?'预览诊断准备':'Preview diagnostic setup',exact:true});
          const confirm=dialog.getByRole('button',{name:zh?'确认准备诊断组件':'Confirm diagnostic setup',exact:true});
          const remove=dialog.getByRole('button',{name:zh?'移除诊断组件':'Remove diagnostic component',exact:true});
          await check.click();await list.getByText(zh?'此处没有准备记录':'No preparation record here',{exact:false}).waitFor();
          await preview.click();await confirm.waitFor({state:'visible'});
          assert.equal(await remove.isVisible(),false);
          await confirm.click();await list.getByText(zh?'诊断组件已准备':'Diagnostic component prepared',{exact:false}).waitFor();
          assert.equal(await confirm.isVisible(),false);assert.equal(await remove.isVisible(),true);
          await dialog.getByRole('button',{name:zh?'关闭':'Close',exact:true}).click();
          await open();await expand();await find.click();
          await check.click();await list.getByText(zh?'诊断组件已准备':'Diagnostic component prepared',{exact:false}).waitFor();
          await remove.click();await list.getByText(zh?'诊断组件已移除':'Diagnostic component removed',{exact:false}).waitFor();
          assert.deepEqual(diagnosticCalls.map(call=>call.action),['lookup','preview','apply','lookup','remove']);
          assert(!String(await dialog.textContent()).includes('connected to mail'));
        }else if(scenario==='profiles'){
          await find.click();await ps.getByText(zh?'找到 1 个配置档':'Found 1 profile.',{exact:false}).waitFor();
          assert.match(await list.textContent(),/research/);
          assert.match(await ps.textContent(),zh?/诊断接入不等于收信接入/:/Diagnostic admission does not connect mail reception/);
          await find.focus();assert.equal(await find.evaluate(e=>e===document.activeElement),true);
          await list.scrollIntoViewIfNeeded();
          const close=await dialog.getByRole('button',{name:zh?'关闭':'Close',exact:true}).boundingBox();
          assert(close && close.y>=0 && close.y+close.height<=page.viewportSize().height);
          if(process.env.UI_EVIDENCE_DIR){fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`profiles-${locale}.png`)});}
          for(const [patch,cn,en] of [
            [{profiles:[]},'未找到可核实','No verifiable profiles'],
            [{state:'runtime_not_discovered',profiles:[]},'标准位置未找到','No Hermes runtime'],
            [{session_verified:true},'无法确认','could not be confirmed'],
            [{profiles:[{...candidate,label:'<img src=x onerror=alert(1)>'}]},'无法确认','could not be confirmed'],
            [{profiles:[candidate,candidate]},'无法确认','could not be confirmed'],
          ]){
            const previous=profileResult;profileResult={...previous,...patch};
            await find.click();await ps.getByText(zh?cn:en,{exact:false}).waitFor();
            assert.equal(await list.isVisible(),false);assert.equal(await find.isEnabled(),true);profileResult=previous;
          }
          profileFailure=true;await find.click();await ps.getByText(zh?'无法确认':'could not be confirmed',{exact:false}).waitFor();
          assert(!String(await dialog.textContent()).includes('/private/secret'));profileFailure=false;
          await find.click();await list.getByText('research',{exact:true}).waitFor();
          // Names stay readable and the dialog contains its content at short widths.
          profileResult.profiles=[{...candidate,label:'r'.repeat(64)}];await find.click();await list.getByText('r'.repeat(64),{exact:true}).waitFor();
          assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1));
        }else{
          profileHold=true;await find.click();
          for(let i=0;i<100&&!profileRelease;i++)await new Promise(r=>setTimeout(r,10));assert(profileRelease);
          assert.equal(await find.isDisabled(),true);
          if(scenario==='profile-race')await dialog.locator('#native-receiver-project').selectOption('B');
          else {await dialog.getByRole('button',{name:zh?'关闭':'Close',exact:true}).click();await open();await expand();}
          await ps.getByText(zh?'尚未查找':'Not checked yet',{exact:false}).waitFor();
          const response=page.waitForResponse(r=>r.url().includes('/hermes/profiles'));profileRelease();await response;
          await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
          assert.match(await ps.textContent(),zh?/尚未查找/:/Not checked yet/);assert.equal(await list.isVisible(),false);
          assert.equal(await find.isEnabled(),true);
          await find.click();await list.getByText('research',{exact:true}).waitFor();
          assert.deepEqual(profileRequests,scenario==='profile-race'?['A','B']:['A','A']);
        }
      }else if(scenario==='states'){
        const backlog=dialog.locator('#native-receiver-backlog');
        assert.match(await backlog.textContent(),zh?/1 条投递结果未知/:/1 alert has an unknown delivery result/);
        assert.doesNotMatch(await backlog.textContent(),zh?/2 条待投递提醒/:/2 pending alerts/);
        for(const [state,cn,en] of [['binding_required','尚未授权','No existing session'],['route_required','未配置匹配','not configured'],['receiver_offline','离线','offline'],['host_unavailable','目前不可用','unavailable'],['check_unconfirmed','尚未确认','could not be confirmed']]){
          next=state;await refresh.click();await status.getByText(zh?cn:en,{exact:false}).waitFor();
          assert(!/接收组件可用。|The receiver component is available\./.test(await status.textContent()));
        }
        if(process.env.UI_EVIDENCE_DIR){fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`${kind}-${locale}.png`)});}
      } else {
        hold=true;await refresh.click();
        for(let i=0;i<100&&!release;i++)await new Promise(r=>setTimeout(r,10));assert(release);
        next='receiver_offline';
        if(scenario==='project-race')await dialog.locator('#native-receiver-project').selectOption('B');
        else {await dialog.getByRole('button',{name:zh?'关闭':'Close',exact:true}).click();await open();}
        await status.getByText(zh?'接收组件离线':'The receiver is offline',{exact:false}).waitFor();
        const response=page.waitForResponse(r=>r.url().includes('/receivers/native'));release();await response;
        await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
        assert.match(await status.textContent(),zh?/离线/:/offline/);
        assert.equal(await refresh.isEnabled(),true);
      }
      if(kind==='deepseek')assert.equal(await dialog.locator('.receiver-profile-discovery').count(),1);
      assert.deepEqual(errors,[]);results.push({kind,locale,scenario,passed:true,checks:requests.length,profileChecks:profileRequests.length});await context.close();
    }
    console.log(JSON.stringify({passed:results.length,results}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
