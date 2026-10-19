require('./browser_sandbox_guard.cjs');
// Exercise unchanged-form retry using real workbench assets and a synthetic API.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({locale: 'zh-CN', viewport: {width: 1200, height: 800}});
    const requests = [];
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== 'mailbox.test') return route.abort();
      if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: {
        projects: [{id: 'project-1', name: 'Synthetic project'}],
        employees: [{id: 'employee-1', name: 'Synthetic employee', kind: 'codex', node_id: 'local', project_id: 'project-1', lifecycle: 'active'}],
        node: {id: 'local', name: 'Local'}, tasks: [], resources: [], memories: [], devices: [], messages: [],
        runtime: {installed: false}, version: '0.8.3-test', fleet: null,
      }});
      if (url.pathname === '/api/workbench/mail-tasks') {
        requests.push(JSON.parse(route.request().postData()));
        return route.fulfill({status: 503, json: {error: {code: 'NETWORK_UNKNOWN', message: 'Synthetic uncertain result'}}});
      }
      if (url.pathname.startsWith('/api/')) return route.fulfill({json: {events: [], notifications: [], revision: 0}});
      const name = path.basename(url.pathname) || 'index.html';
      return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.addInitScript(() => {
      const original = window.fetch.bind(window);
      window.fetch = (url, opts = {}) => url === '/api/workbench/changes'
        ? Promise.resolve(new Response(new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode('{"revision":0}\n')); opts.signal?.addEventListener('abort', () => c.close(), {once: true});}}), {headers: {'Content-Type': 'application/x-ndjson'}}))
        : original(url, opts);
    });
    await page.goto('http://mailbox.test/#token=synthetic');
    await page.locator('[data-view="tasks"]').first().click();
    await page.locator('[data-action="new-task"]').first().click();
    await page.locator('#task-title').fill('Synthetic handoff');
    await page.locator('#task-prompt').fill('Read one document');
    const submit = page.getByRole('button', {name: '派发任务'});
    await submit.click();
    await page.waitForFunction(() => document.querySelector('#task-title') && !document.querySelector('button[type="submit"]')?.dataset.loading);
    await submit.click();
    await page.waitForFunction(() => document.querySelector('#task-title') && !document.querySelector('button[type="submit"]')?.dataset.loading);
    await page.locator('#task-prompt').fill('Read a different document');
    await submit.click();
    await page.waitForFunction(() => document.querySelector('#task-title') && !document.querySelector('button[type="submit"]')?.dataset.loading);
    assert.equal(requests.length, 3);
    assert.match(requests[0].request_id, /^task-ui-[a-f0-9]{32}$/);
    assert.equal(requests[0].request_id, requests[1].request_id);
    assert.notEqual(requests[1].request_id, requests[2].request_id);
    assert.deepEqual(errors, []);
    console.log('unchanged retry and edited request PASS');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
