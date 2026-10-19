require('./browser_sandbox_guard.cjs');
// Real workbench UI assets with a synthetic owner API; no SSH or live credentials.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    for (const locale of ['zh-CN', 'en-US']) for (const width of [320, 900]) {
      const context = await browser.newContext({locale, viewport: {width, height: 600}});
      const page = await context.newPage();
      const errors = [];
      let enabled = false;
      let restoreError = true;
      let starts = 0;
      let stops = 0;
      page.on('pageerror', error => errors.push(error.message));
      page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: {
          projects: [], employees: [], node: {id: 'device'}, tasks: [], resources: [], memories: [],
          devices: [], messages: [], runtime: {installed: false}, version: '0.8.3-test',
          fleet: {listener: null, remote: null}, human_forward: {enabled, port: enabled ? 49182 : null,
            ...(restoreError ? {error: {code: 'HUMAN_FORWARD_RESTORE_FAILED'}} : {})},
        }});
        if (url.pathname === '/api/workbench/human-forward/start') {
          starts++;
          enabled = true;
          restoreError = false;
          return route.fulfill({json: {enabled, port: 49182}});
        }
        if (url.pathname === '/api/workbench/human-forward/stop') {
          stops++;
          enabled = false;
          return route.fulfill({json: {stopped: true}});
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
      if (width < 600) await page.locator('#sidebar-toggle').click();
      await page.locator('[data-view="devices"]').first().click();
      await page.getByText(locale === 'zh-CN' ? /上次启动后未能恢复 Human 入口/ : /Human access did not recover after restart/).waitFor();
      await page.getByRole('button', {name: locale === 'zh-CN' ? '启用 SSH 转发入口' : 'Enable SSH forward target'}).click();
      await page.getByText('ssh -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127.0.0.1:49180:127.0.0.1:49182 user@home-host').waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.getByRole('button', {name: locale === 'zh-CN' ? '停止 Human 远程入口' : 'Stop remote Human access'}).click();
      await page.getByRole('button', {name: locale === 'zh-CN' ? '启用 SSH 转发入口' : 'Enable SSH forward target'}).waitFor();
      assert.equal(starts, 1);
      assert.equal(stops, 1);
      assert.deepEqual(errors, []);
      console.log(`${locale}/${width} PASS`);
      await context.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
