require('./browser_sandbox_guard.cjs');
// Real workbench UI; synthetic API proves a LAN hint never joins by itself.
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
      let joined = null;
      page.on('pageerror', error => errors.push(error.message));
      page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        const fleet = {saved_pairing: Boolean(joined), listener: {base_url: 'https://10.0.0.18:49002'}, error: null,
          remote: joined ? {paired: true, base_url: joined.base_url, projects: [], mappings: {}, worker: {errors: {}}} : null};
        if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: {
          projects: [], employees: [], node: {id: 'device'}, tasks: [], resources: [], memories: [],
          devices: [], messages: [], runtime: {installed: false}, version: '0.8.3-test', fleet
        }});
        if (url.pathname === '/api/workbench/fleet/discover') return route.fulfill({json: {
          trusted: false, devices: [{base_url: 'https://192.168.1.12:49001', verified: false}]
        }});
        if (url.pathname === '/api/workbench/fleet/join') {
          joined = JSON.parse(route.request().postData()).invite;
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
      await page.getByText(/ssh -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127\.0\.0\.1:49181:10\.0\.0\.18:49002 USER@SSH_HOST/).waitFor();
      await page.getByRole('button', {name: locale === 'zh-CN' ? '粘贴邀请并加入' : 'Paste invitation to join'}).click();
      await page.getByRole('button', {name: locale === 'zh-CN' ? '查找同网主控' : 'Find local coordinators'}).click();
      await page.getByRole('button', {name: 'https://192.168.1.12:49001'}).click();
      assert.equal(joined, null, 'address without a full invitation must not pair');
      assert.equal(await page.locator('#fleet-join-invitation').inputValue(), '');
      await page.locator('#fleet-join-invitation').fill(JSON.stringify({
        base_url: 'https://192.168.1.10:49001', fingerprint: 'sha256:synthetic',
        invite_id: 'one-use', invite_secret: 'test-secret'
      }));
      await page.getByRole('button', {name: 'https://192.168.1.12:49001'}).click();
      const prepared = JSON.parse(await page.locator('#fleet-join-invitation').inputValue());
      assert.equal(prepared.base_url, 'https://192.168.1.12:49001');
      assert.equal(prepared.fingerprint, 'sha256:synthetic');
      assert.equal(joined, null, 'discovery must not pair without explicit Human submit');
      await page.getByRole('button', {name: locale === 'zh-CN' ? '确认邀请并加入' : 'Confirm invitation and join'}).click();
      await page.getByText(locale === 'zh-CN' ? '设备已配对' : 'Device paired', {exact: true}).waitFor();
      assert.deepEqual(joined, prepared);
      await page.setViewportSize({width: 320, height: 640});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      console.log(`${locale} PASS`);
      await context.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
