require('./browser_sandbox_guard.cjs');
// Real workbench assets, synthetic owner API. Never connects to a real home.
// PLAYWRIGHT_MODULE and CHROME_PATH may point to an existing local test runtime.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));
const fixture = {
  projects: ['A', 'B'].map(id => ({id, name: `Project ${id}`, path: `/synthetic/${id}`})),
  employees: [{id:'worker', name:'Synthetic Codex', kind:'codex', node_id:'local',
    lifecycle:'active', connection_type:'app', project_id:'A', project_ids:['A','B'],
    project_roles:{A:'A original',B:'B original'}, auth_status:'unknown'}],
  node:{id:'local',name:'Synthetic device',is_local:true}, devices:[],
  tasks:[], messages:[], resources:[], memories:[], runtime:{installed:false},
  version:'0.8.3-test', bootstrap_version:1
};
const cases = ['configuration', 'round-trip', 'records', 'responsibility', 'closed-form', 'failure-and-revoke'];
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  const results=[];
  try {
    for (const locale of ['zh-CN','en-US']) for (const scenario of cases) {
      const context=await browser.newContext({locale,viewport:{width:locale==='zh-CN'?1200:900,height:locale==='zh-CN'?800:600}});
      const page=await context.newPage(); page.setDefaultTimeout(6000);
      const requests=[], errors=[]; let held=null, hold=null, fail=false;
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(url.host!=='mailbox.test')return route.abort();
        if(url.pathname==='/api/workbench/bootstrap')return route.fulfill({json:fixture});
        if(url.pathname.includes('/mailbox/sessions') || url.pathname.endsWith('/role')) {
          const method=req.method(), body=method==='POST'?req.postDataJSON():null;
          const project=body?.project_id || url.searchParams.get('project_id');
          requests.push({method,path:url.pathname,body,project});
          const sessions=[{id:`session-${project}`,label:`Issued-${project}`,active:true,expires_at:'2030-01-01'}];
          const json=method==='GET'?{sessions}:method==='POST'&&url.pathname.endsWith('/sessions')?
            {config:{mcpServers:{'agent-mailbox-project':{command:'/synthetic/mailbox',args:['mailbox-mcp','--session-file',`/synthetic/${project}.json`]}}}}:{};
          if(hold && hold({method,path:url.pathname,project})) {
            hold=null; await new Promise(resolve=>{held=resolve;});
          }
          if(fail&&method==='POST')return route.fulfill({status:503,json:{error:{code:'connection_failed',message:'synthetic lost acknowledgement'}}});
          return route.fulfill({json});
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
      const zh=locale==='zh-CN',dialog=page.getByRole('dialog');
      const button=(cn,en)=>dialog.getByRole('button',{name:zh?cn:en,exact:true});
      const open=()=>page.getByRole('button',{name:zh?'邮箱接入':'Connect mailbox',exact:true}).click();
      const project=dialog.locator('#mail-session-project'), config=dialog.locator('.mail-session-config');
      const generate=button('生成邮箱接入配置','Generate mailbox connection');
      const ready=label=>dialog.getByText(label,{exact:false}).waitFor();
      const flush=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const waitHeld=async()=>{for(let n=0;n<100&&!held;n++)await new Promise(r=>setTimeout(r,10));assert(held,'synthetic response was not held');};
      const release=async()=>{await waitHeld(); const response=page.waitForResponse(r=>r.url().includes('/api/workbench/')&&r.request().method()!== 'OPTIONS'); const done=held; held=null;done();await response;await flush();};
      if(scenario==='records')hold=r=>r.method==='GET'&&r.project==='A';
      await open();
      if(scenario!=='records')await ready('Issued-A');
      if(scenario==='configuration'||scenario==='round-trip'||scenario==='closed-form') {
        hold=r=>r.method==='POST'&&r.project==='A';
        await generate.click();await waitHeld();
        if(scenario==='closed-form') { await button('关闭','Close').click();await open(); }
        else { await project.selectOption('B');await ready('Issued-B');if(scenario==='round-trip'){await project.selectOption('A');await ready('Issued-A');} }
        await release();
        assert.equal(await config.innerText(),'','late configuration crossed a project/form boundary');
      } else if(scenario==='records') {
        await waitHeld();await project.selectOption('B');await ready('Issued-B');await release();
        assert(!(await dialog.innerText()).includes('Issued-A'),'late records replaced current project records');
        assert((await dialog.innerText()).includes('Issued-B'));
      } else if(scenario==='responsibility') {
        await dialog.locator('#mail-session-role').fill('A submitted');
        hold=r=>r.method==='POST'&&r.path.endsWith('/role');
        await button('保存项目职责','Save project responsibility').click();await waitHeld();
        await project.selectOption('B');await ready('Issued-B');await dialog.locator('#mail-session-role').fill('B unsaved');
        await release();await project.selectOption('A');await ready('Issued-A');
        assert.equal(await dialog.locator('#mail-session-role').inputValue(),'A submitted');
        await project.selectOption('B');await ready('Issued-B');
        assert.equal(await dialog.locator('#mail-session-role').inputValue(),'B original');
      } else {
        await generate.click();await page.waitForFunction(()=>document.querySelector('.mail-session-config').textContent.includes('/synthetic/A.json'));
        await page.waitForFunction(()=>!Array.from(document.querySelectorAll('dialog button')).find(b=>/^(生成邮箱接入配置|Generate mailbox connection)$/.test(b.textContent)).disabled);
        fail=true;await generate.click();await dialog.getByRole('alert').waitFor();
        assert.equal(await config.innerText(),'','failed issue retained stale configuration');
        assert.match(await dialog.getByRole('alert').innerText(),zh?/未确认/:/unconfirmed/i);
        fail=false;page.once('dialog',d=>d.accept());await button('撤销','Revoke').click();
        await flush();assert.equal(requests.filter(r=>r.method==='DELETE').length,1);
      }
      assert.deepEqual(errors,[]);results.push({locale,scenario,passed:true});
      await context.close();
    }
    console.log(JSON.stringify({passed:results.length,results}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
