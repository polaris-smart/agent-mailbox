require('./browser_sandbox_guard.cjs');
// Exercise the actual Human page with synthetic project data and no real mailbox.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    for (const width of [375, 900]) {
      const context = await browser.newContext({viewport: {width, height: 700}});
      const page = await context.newPage();
      const errors = [];
      const decisions = [];
      let taskStatus = 'review';
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(() => {
        sessionStorage.setItem('agent-mailbox.human.token', 'synthetic-human-token');
        const original = window.fetch.bind(window);
        window.fetch = (url, opts = {}) => url === '/api/workbench/human/changes'
          ? Promise.resolve(new Response(new ReadableStream({start(c) {
              c.enqueue(new TextEncoder().encode('{"ready":true}\n'));
              opts.signal?.addEventListener('abort', () => c.close(), {once: true});
            }}), {headers: {'Content-Type': 'application/x-ndjson'}}))
          : original(url, opts);
      });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname === '/api/workbench/human/self') return route.fulfill({json: {human_id: 'human_owner', display_name: 'Owner'}});
        if (url.pathname === '/api/workbench/human/spaces') return route.fulfill({json: {spaces: [{id: 'space_private', name: 'Private', kind: 'private', role: 'owner'}]}});
        if (url.pathname === '/api/workbench/human/projects') return route.fulfill({json: {projects: [{id: 'project_one', name: 'Review project', space_id: 'space_private', kind: 'private', role: 'owner', can_review: 1}]}});
        if (url.pathname === '/api/workbench/human/projects/project_one/tasks') return route.fulfill({json: {tasks: [{id: 'task_one', title: 'Synthetic task', status: taskStatus}]}});
        if (url.pathname === '/api/workbench/human/projects/project_one/messages') return route.fulfill({json: {messages: []}});
        if (url.pathname === '/api/workbench/human/projects/project_one/resources/manifest') return route.fulfill({json: {resources: []}});
        if (url.pathname === '/api/workbench/human/projects/project_one/tasks/task_one/review') {
          if (route.request().method() === 'GET') return route.fulfill({json: {
            task_id: 'task_one', title: 'Synthetic task', status: 'review', summary: 'Synthetic delivery',
            verification: {status: 'employee_report', notes: 'Not independently verified'},
            files: [], diff: '', capture_error: null,
          }});
          decisions.push(route.request().postDataJSON());
          taskStatus = decisions.at(-1).decision === 'accept' ? 'done' : 'failed';
          return route.fulfill({json: {task_id: 'task_one', status: taskStatus}});
        }
        if (url.pathname.startsWith('/api/')) return route.fulfill({status: 404, json: {error: {message: 'Unexpected API'}}});
        const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
        return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
      });
      await page.goto('http://mailbox.test/human');
      await page.getByRole('button', {name: 'Review project'}).click();
      await page.getByRole('button', {name: '查看交付并验收'}).click();
      await page.getByText('Synthetic delivery').waitFor();
      if (process.env.UI_SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.UI_SCREENSHOT_DIR, `human-review-${width}.png`)});
      }
      assert.equal(await page.locator('#review-accept').isDisabled(), true);
      await page.locator('#review-checked').check();
      assert.equal(await page.locator('#review-accept').isEnabled(), true);
      if (width === 375) {
        await page.locator('#review-accept').click();
        await page.getByText('验收已通过。').waitFor();
        assert.deepEqual(decisions, [{decision: 'accept', note: ''}]);
      } else {
        await page.locator('#review-reject').click();
        await page.getByText('退回时请填写具体意见。').waitFor();
        assert.deepEqual(decisions, []);
        await page.locator('#review-note').fill('Please add evidence');
        await page.locator('#review-reject').click();
        await page.getByText('任务已退回。').waitFor();
        assert.deepEqual(decisions, [{decision: 'reject', note: 'Please add evidence'}]);
      }
      assert.equal(await page.locator('#review-dialog').isVisible(), false);
      assert.equal(await page.getByRole('button', {name: '查看交付并验收'}).count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      assert.deepEqual(errors, []);
      console.log(`${width}px owner review PASS`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
