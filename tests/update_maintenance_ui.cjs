require('./browser_sandbox_guard.cjs');
// Real workbench assets with a synthetic owner backend; no production connection.
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
      page.on('pageerror', error => errors.push(error.message));
      const maintenance = {paused: true, active_tasks: [], durable_mail_tasks: [{id: 'task-1', title: 'Synthetic long handoff', status: 'running'}], queued_count: 0, pending_claims: []};
      const snapshot = {projects: [{id: 'project-1', name: 'Synthetic project'}], employees: [], node: {id: 'local', name: 'Synthetic device'}, tasks: [], resources: [], memories: [], devices: [], messages: [], runtime: {installed: false}, version: '0.8.3-test', update_maintenance: maintenance};
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: snapshot});
        if (url.pathname === '/api/workbench/updates/status') return route.fulfill({json: {current_version: '0.8.3-test', channel: 'stable', maintenance, installation: {instructions: []}, nodes: []}});
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
      const banner = page.locator('#update-maintenance-banner');
      await banner.waitFor();
      assert.equal(await banner.isVisible(), true);
      assert.match(await banner.textContent(), locale === 'zh-CN' ? /暂停接单与邮箱写入/ : /Claims and mailbox writes paused/);
      await page.locator('[data-view="about"]').first().click();
      const section = page.locator('.update-section');
      await section.getByText(locale === 'zh-CN' ? /保留 1 项已接单邮箱任务/ : /1 accepted mail task\(s\) remain/).waitFor();
      assert(await section.getByRole('button', {name: locale === 'zh-CN' ? '恢复接单与写入' : 'Resume claims and writes'}).isVisible());
      assert(await section.evaluate(node => node.scrollWidth <= node.clientWidth + 1));
      assert.deepEqual(errors, []);
      if (process.env.UI_EVIDENCE_DIR) {
        fs.mkdirSync(process.env.UI_EVIDENCE_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.UI_EVIDENCE_DIR, `update-maintenance-${locale}.png`)});
      }
      console.log(`${locale} PASS`);
      await context.close();
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
