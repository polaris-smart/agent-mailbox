require('./browser_sandbox_guard.cjs');
// Real workbench UI assets; only the owner API is synthetic.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    for (const locale of ['zh-CN', 'en-US']) {
      const context = await browser.newContext({locale, viewport: {width: 900, height: 600}});
      const page = await context.newPage();
      const errors = [];
      let connected = false;
      let submitted;
      page.on('pageerror', error => errors.push(error.message));
      page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        const fleet = {saved_pairing: true, listener: null, error: connected ? null : {code: 'DEVICE_RESTORE_FAILED', message: 'Old address unreachable'}, remote: connected ? {paired: true, base_url: 'https://localhost:8765', projects: [], mappings: {}, worker: {errors: {}}} : null};
        if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: {projects: [], employees: [], node: {id: 'device'}, tasks: [], resources: [], memories: [], devices: [], messages: [], runtime: {installed: false}, version: '0.8.3-test', fleet}});
        if (url.pathname === '/api/workbench/fleet/relocate') {
          submitted = JSON.parse(route.request().postData());
          connected = true;
          return route.fulfill({json: fleet});
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
      await page.locator('[data-view="devices"]').first().click();
      const label = locale === 'zh-CN' ? '更改主控地址' : 'Change coordinator address';
      await page.getByRole('button', {name: label}).click();
      await page.locator('#fleet-new-address').fill('https://localhost:8765');
      await page.getByRole('button', {name: locale === 'zh-CN' ? '验证并使用新地址' : 'Verify and use new address'}).click();
      await page.getByText(locale === 'zh-CN' ? '设备已配对' : 'Device paired').waitFor();
      assert.deepEqual(submitted, {base_url: 'https://localhost:8765'});
      assert.deepEqual(errors, []);
      console.log(`${locale} PASS`);
      await context.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
