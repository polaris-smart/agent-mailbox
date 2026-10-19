require('./browser_sandbox_guard.cjs');
// Real shipped UI, intercepted synthetic owner API; no real projects or credentials.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));
const fixture = {
  projects:[{id:'A',name:'Synthetic project',path:'/synthetic/project'}],
  employees:[{id:'worker',name:'Synthetic Codex',kind:'codex',node_id:'local',lifecycle:'active',
    connection_type:'app',project_id:'A',project_ids:['A'],project_roles:{},auth_status:'unknown'}],
  node:{id:'local',name:'Synthetic device',is_local:true},devices:[],tasks:[],messages:[],resources:[],memories:[],
  runtime:{installed:false},version:'0.8.3-test',bootstrap_version:1,
};
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const results=[];
  try {
    for(const locale of ['zh-CN','en-US'])for(const scenario of ['install-remove','identity-unavailable','lost-ack','conflict','closed-form','history','replace','recover-before','recover-after','recover-conflict','recover-closed']) {
      const context=await browser.newContext({locale,viewport:{width:locale==='zh-CN'?1200:900,height:locale==='zh-CN'?800:600}});
      const page=await context.newPage();page.setDefaultTimeout(6000);
      const zh=locale==='zh-CN',requests=[],errors=[];let records=[],release=null;
      const plan={plan_id:'a'.repeat(32),employee_id:'worker',project_id:'A',session_id:'session-A',thread_id:'thread-A',
        workspace:'/synthetic/project',scope:'workspace_codex_sessions',expires_at:Date.now()/1000+600,connection_verified:false};
      if(scenario==='history')records=[{...plan,plan_id:'b'.repeat(32),state:'preview_expired'}, {...plan,state:'written_waiting_for_host'}];
      if(scenario.startsWith('recover-'))records=[{...plan,state:'outcome_requires_review'}];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(url.host!=='mailbox.test')return route.abort();
        if(url.pathname==='/api/workbench/bootstrap')return route.fulfill({json:fixture});
        if(url.pathname==='/api/workbench/mailbox/sessions')return route.fulfill({json:{sessions:[{id:'session-A',label:'Synthetic issued session',active:true,expires_at:'2030-01-01'}]}});
        if(url.pathname==='/api/workbench/mailbox/receivers/codex') {
          const body=req.postDataJSON();requests.push(body);
          assert.equal(body.action,'sessions');
          return route.fulfill({json:{sessions:[{id:'thread-A',name:'Original chat',cwd:'/synthetic/project'}, {id:'thread-B',name:'Reconnected chat',cwd:'/synthetic/project'}],truncated:false}});
        }
        if(url.pathname==='/api/workbench/mailbox/installers/codex') {
          const body=req.postDataJSON();requests.push(body);
          assert.equal(body.employee_id,'worker');assert.equal(body.project_id,'A');assert.equal(body.session_id,'session-A');
          assert(!('command' in body)&&!('workspace' in body)&&!('token' in body));
          if(body.action==='history')return route.fulfill({json:{plans:records}});
          if(body.action==='replaceable')return route.fulfill({json:{plans:[{...plan,plan_id:'c'.repeat(32),session_id:'expired-old-session',state:'written_waiting_for_host'}]}});
          if(body.action==='recover') {
            assert.equal(body.confirmed,true);assert.equal(body.plan_id,plan.plan_id);
            if(scenario==='recover-closed')await new Promise(resolve=>{release=resolve;});
            records=[{...plan,state:scenario==='recover-after'?'written_waiting_for_host':scenario==='recover-conflict'?'configuration_changed':'closed_at_original',
              recovery_observation:scenario==='recover-after'?'current_matches_after':scenario==='recover-conflict'?null:'current_matches_before'}];
            return route.fulfill({json:records[0]});
          }
          if(body.action==='preview_replace') {
            assert.equal(body.plan_id,'c'.repeat(32));assert.equal(body.thread_id,'thread-A');assert(!body.confirmed);
            plan.replaces='c'.repeat(32);
            return route.fulfill({json:{...plan,state:'confirmation_required'}});
          }
          if(body.action==='preview') {
            assert.equal(body.thread_id,'thread-A');assert(!body.confirmed);
            if(scenario==='conflict')return route.fulfill({status:409,json:{error:{code:'MAILBOX_INSTALL_UNAVAILABLE',message:'installer_existing_mailbox_requires_review'}}});
            if(scenario==='closed-form')await new Promise(resolve=>{release=resolve;});
            return route.fulfill({json:{...plan,state:'confirmation_required'}});
          }
          if(body.action==='apply') {
            assert.equal(body.confirmed,true);assert.equal(body.plan_id,plan.plan_id);
            records=[{...plan,state:'written_waiting_for_host'}];
            if(scenario==='lost-ack')return route.fulfill({status:503,json:{error:{code:'MAILBOX_INSTALL_UNAVAILABLE',message:'installer_operation_unconfirmed'}}});
            return route.fulfill({json:records[0]});
          }
          if(body.action==='verify') {
            assert.equal(body.thread_id,'thread-B');assert.equal(body.plan_id,plan.plan_id);
            assert(!('confirmed' in body)&&!('tool' in body)&&!('arguments' in body));
            if(scenario==='identity-unavailable')return route.fulfill({status:409,json:{error:{code:'MAILBOX_INSTALL_UNAVAILABLE',message:'receiver_identity_unconfirmed'}}});
            return route.fulfill({json:{...records[0],connection_verified:true,verified_thread_id:'thread-B',
              checked_at:Date.now()/1000,verification_scope:'mailbox_identity_only'}});
          }
          assert.equal(body.action,'remove');assert.equal(body.confirmed,true);assert.equal(body.plan_id,plan.plan_id);
          records=[{...plan,state:'removed'}];return route.fulfill({json:records[0]});
        }
        if(url.pathname.startsWith('/api/'))return route.fulfill({json:{events:[],notifications:[],capabilities:[],revision:0}});
        const name=path.basename(url.pathname)||'index.html';
        return route.fulfill({body:fs.readFileSync(path.join(assets,name)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
      });
      await page.addInitScript(()=>{
        const original=window.fetch.bind(window);
        window.fetch=(url,opts={})=>url==='/api/workbench/changes'?Promise.resolve(new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('{"revision":0}\n'));opts.signal?.addEventListener('abort',()=>c.close(),{once:true});}}),{headers:{'Content-Type':'application/x-ndjson'}})):original(url,opts);
      });
      await page.goto('http://mailbox.test/#token=synthetic-only');
      await page.locator('[data-view="employees"]').first().dispatchEvent('click');
      const dialog=page.getByRole('dialog');
      const button=(cn,en)=>dialog.getByRole('button',{name:zh?cn:en,exact:true,includeHidden:true});
      const open=async()=>{
        await page.getByRole('button',{name:zh?'邮箱接入':'Connect mailbox',exact:true}).click();
        await button('项目接入设置','Project connection setup').click();
      };
      await open();
      const apply=button('确认安装到此项目','Confirm project installation');
      const load=button('读取已打开的会话','Load open sessions');
      const remove=button('移除此项目配置','Remove this project configuration');
      const status=dialog.getByRole('status');
      if(scenario.startsWith('recover-')) {
        const recover=button('核对并恢复记录','Check and recover record');
        await recover.waitFor();
        assert.equal(await apply.isDisabled(),true);assert.equal(await remove.isDisabled(),true);
        page.once('dialog',d=>d.dismiss());await recover.click();
        assert.equal(requests.filter(r=>r.action==='recover').length,0);
        page.once('dialog',d=>d.accept());await recover.click();
        if(scenario==='recover-closed') {
          for(let i=0;i<100&&!release;i++)await new Promise(r=>setTimeout(r,10));assert(release);
          await button('关闭','Close').click();await open();release();
          await page.waitForResponse(r=>r.url().includes('/installers/codex')&&r.request().postDataJSON().action==='recover');
          assert(!/旧计划已结束|This plan is closed/.test(await status.textContent()));
        } else {
          const pattern=scenario==='recover-after'?/项目配置已写入|Project configuration written/:scenario==='recover-before'?/旧计划已结束|This plan is closed/:/配置.*变化|configuration.*changed/i;
          await page.waitForFunction(p=>new RegExp(p,'i').test(document.querySelector('dialog [role=status]').textContent),pattern.source);
          if(scenario==='recover-before')assert.match(await status.textContent(),/不证明宿主从未加载|does not prove the host never loaded/);
          assert.equal(await apply.isDisabled(),true);
          if(process.env.UI_EVIDENCE_DIR) {
            fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});
            await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`${scenario}-${locale}.png`)});
          }
        }
        assert.equal(requests.filter(r=>r.action==='recover').length,1);
        assert.equal(requests.filter(r=>['apply','remove','verify'].includes(r.action)).length,0);
        assert.deepEqual(errors,[]);results.push({locale,scenario,passed:true});await context.close();continue;
      }
      if(scenario==='replace') {
        await load.click();
        await dialog.locator('#mail-install-thread').selectOption('thread-A');
        await button('读取可替换的旧接入','Load previous connection to replace').click();
        await dialog.locator('#mail-install-replace').waitFor();
        const replace=button('预览替换为当前凭据','Preview replacement with current credential');
        assert.equal(await replace.isDisabled(),true);
        await dialog.locator('#mail-install-replace').selectOption('c'.repeat(32));
        await replace.click();
        const confirm=button('确认替换此项目接入','Confirm project connection replacement');
        await confirm.waitFor();assert.equal(await confirm.isDisabled(),true);
        await dialog.getByText(zh?'本次替换旧接入，保留其他配置；不撤销旧凭据，也不自动重连宿主。':'This replaces the previous connection and preserves other configuration. It does not revoke the old credential or reconnect the host automatically.',{exact:true}).waitFor();
        await dialog.locator('#mail-install-consent').check();await confirm.click();
        await page.waitForFunction(()=>/项目配置已写入|Project configuration written/.test(document.querySelector('dialog [role=status]').textContent));
        assert.equal(requests.filter(r=>r.action==='apply').length,1);
        assert.equal(requests.filter(r=>r.action==='preview').length,0);
        assert.equal(requests.filter(r=>r.action==='preview_replace').length,1);
        assert.equal(await dialog.locator('#mail-install-replace').isHidden(),true);
        if(process.env.UI_EVIDENCE_DIR) {
          fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});
          await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`replacement-${locale}.png`)});
        }
        assert.deepEqual(errors,[]);results.push({locale,scenario,passed:true});await context.close();continue;
      }
      if(scenario==='history') {
        await dialog.locator('#mail-install-history').waitFor();
        await dialog.locator('#mail-install-history').selectOption(plan.plan_id);
        assert.equal(await apply.isDisabled(),true);assert.equal(await remove.isDisabled(),false);
      } else {
        await load.click();
        await dialog.locator('#mail-install-thread option[value="thread-A"]').waitFor({state:'attached'});
        assert.equal(await dialog.locator('#mail-install-thread').inputValue(),'');
        assert.equal(await apply.isDisabled(),true);
        await dialog.locator('#mail-install-thread').selectOption('thread-A');
        await button('检查并预览','Check and preview').click();
        if(scenario==='closed-form') {
          for(let i=0;i<100&&!release;i++)await new Promise(r=>setTimeout(r,10));assert(release);
          await button('关闭','Close').click();await open();release();
          await page.waitForResponse(r=>r.url().includes('/installers/codex')&&r.request().postDataJSON().action==='preview');
          assert.equal(await apply.isDisabled(),true);
          assert.equal(await dialog.locator('#mail-install-consent').isChecked(),false);
        } else if(scenario==='conflict') {
          await dialog.getByRole('alert').waitFor();assert.equal(await apply.isDisabled(),true);
        } else {
          await dialog.getByText(zh?'检查已完成，等待你确认安装范围。':'Checks complete. Confirm the installation scope.',{exact:true}).waitFor();
          assert.equal(await apply.isDisabled(),true);
          await dialog.locator('#mail-install-consent').check();await apply.click();
          if(scenario==='lost-ack') {
            await dialog.getByRole('alert').waitFor();assert.equal(await apply.isDisabled(),true);
            await button('刷新接入记录','Refresh installation record').click();
          }
          await page.waitForFunction(()=>/项目配置已写入|Project configuration written/.test(document.querySelector('dialog [role=status]').textContent));
          assert.equal(requests.filter(r=>r.action==='apply').length,1);
          assert.equal(await apply.isDisabled(),true);
          if(['install-remove','identity-unavailable'].includes(scenario)) {
            const verify=button('检查此会话身份',"Check this session's identity");
            await load.click();
            await dialog.locator('#mail-install-thread option[value="thread-B"]').waitFor({state:'attached'});
            assert.equal(await verify.isDisabled(),true);
            await dialog.locator('#mail-install-thread').selectOption('thread-B');
            await verify.click();
            if(scenario==='identity-unavailable') {
              await dialog.getByRole('alert').waitFor();
              assert(!/本次检查：此会话|Current check: this session/.test(await status.textContent()));
              assert.equal(requests.filter(r=>r.action==='apply').length,1);
            } else {
              await page.waitForFunction(()=>/本次检查：此会话|Current check: this session/.test(document.querySelector('dialog [role=status]').textContent));
              assert.match(await status.textContent(),/不代表通知已送达|not notification delivery/);
              await dialog.locator('#mail-install-thread').selectOption('thread-A');
              assert(!/本次检查：此会话|Current check: this session/.test(await status.textContent()));
              await Promise.all([page.waitForResponse(r=>r.url().includes('/installers/codex')&&r.request().postDataJSON().action==='history'), button('刷新接入记录','Refresh installation record').click()]);
              assert(!/本次检查：此会话|Current check: this session/.test(await status.textContent()));
            }
          }
          if(process.env.UI_EVIDENCE_DIR&&scenario==='install-remove') {
            fs.mkdirSync(process.env.UI_EVIDENCE_DIR,{recursive:true});
            await page.screenshot({path:path.join(process.env.UI_EVIDENCE_DIR,`installer-${locale}.png`)});
          }
        }
      }
      if(['install-remove','history'].includes(scenario)) {
        page.once('dialog',d=>d.accept());await remove.click();
        await page.waitForFunction(()=>/本次项目配置已移除|This project configuration was removed/.test(document.querySelector('dialog [role=status]').textContent));
        assert.equal(requests.filter(r=>r.action==='remove').length,1);
      }
      assert.deepEqual(errors,[]);results.push({locale,scenario,passed:true});await context.close();
    }
    console.log(JSON.stringify({passed:results.length,results}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
