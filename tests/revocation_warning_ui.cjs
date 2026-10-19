require('./browser_sandbox_guard.cjs');
// Isolated browser exercise of the owner UI. All HTTP is intercepted; no live home or host is touched.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    for (const locale of ['zh-CN', 'en-US']) for (const uncertain of [true, false]) {
      const context = await browser.newContext({locale, viewport: {width: 900, height: 600}});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const task = {id: 'task-1', project_id: 'project-1', assignee_id: 'worker-1', title: 'Synthetic task', prompt: 'Synthetic instructions', status: 'cancelled', execution_mode: 'mailbox', created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z'};
      const warning = {id: 'event-1', task_id: task.id, type: 'external_stop_unconfirmed', message: 'Synthetic external stop uncertainty', created_at: '2026-10-13T00:00:01Z'};
      const snapshot = {projects: [{id: 'project-1', name: 'Synthetic project'}], employees: [{id: 'worker-1', name: 'Synthetic worker', kind: 'codex', node_id: 'local', lifecycle: 'retired', project_id: 'project-1', project_ids: ['project-1'], project_roles: {}}], node: {id: 'local', name: 'Synthetic device'}, tasks: [task], resources: [], memories: [], devices: [], messages: [], runtime: {installed: false}, version: '0.8.3-test'};
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname === '/api/workbench/bootstrap') return route.fulfill({json: snapshot});
        if (url.pathname === '/api/workbench/tasks/task-1') return route.fulfill({json: {task, events: uncertain ? [warning] : [], permissions: [], links: []}});
        if (url.pathname === '/api/workbench/tasks/task-1/delivery') return route.fulfill({json: {delivery: {}}});
        if (url.pathname === '/api/workbench/governance') return route.fulfill({json: {events: uncertain ? [{id: 'governance-1', type: 'mail_task_stop_unconfirmed', project_id: 'project-1', task_id: task.id, employee_id: 'worker-1', actor: 'runtime', reason: 'Synthetic reason', created_at: '2026-10-13T00:00:01Z'}] : []}});
        if (url.pathname.startsWith('/api/')) return route.fulfill({json: {events: [], notifications: [], capabilities: [], revision: 0}});
        const name = path.basename(url.pathname) || 'index.html';
        return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
      });
      await page.addInitScript(() => {
        sessionStorage.setItem('agent-mailbox.workbench.project', 'project-1');
        const fetchOriginal = window.fetch.bind(window);
        window.fetch = (url, options = {}) => url === '/api/workbench/changes'
          ? Promise.resolve(new Response(new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode('{"revision":0}\n')); options.signal?.addEventListener('abort', () => controller.close(), {once: true});}}), {headers: {'Content-Type': 'application/x-ndjson'}}))
          : fetchOriginal(url, options);
      });
      await page.goto('http://mailbox.test/#token=synthetic');
      await page.locator('[data-view="tasks"]').first().click();
      await page.locator('[data-task="task-1"]').first().click();
      const alert = page.locator('#detail-dialog [role="alert"].approval-panel');
      if (uncertain) {
        await alert.waitFor();
        assert.match(await alert.textContent(), locale === 'zh-CN' ? /宿主侧是否停工无法确认/ : /Whether work stopped in the host is unknown/);
        assert.match(await alert.textContent(), locale === 'zh-CN' ? /不代表工作仍在运行/ : /does not mean the work is still running/);
        assert(await alert.evaluate(node => node.getBoundingClientRect().width > 0));
        if (process.env.UI_EVIDENCE_DIR) {
          fs.mkdirSync(process.env.UI_EVIDENCE_DIR, {recursive: true});
          await page.screenshot({path: path.join(process.env.UI_EVIDENCE_DIR, `revocation-warning-${locale}.png`)});
        }
      } else assert.equal(await alert.count(), 0);
      await page.locator('#detail-dialog').getByRole('button', {name: locale === 'zh-CN' ? '关闭任务详情' : 'Close task details'}).click();
      await page.locator('[data-view="members"]').first().click();
      const history = page.locator('#governance-panel');
      if (uncertain) await history.getByText(locale === 'zh-CN' ? /外部停工待核实/ : /External work needs checking/).waitFor({timeout: 5000}).catch(async error => {console.error('page-errors:', errors, 'root:', (await page.locator('#view-root').textContent()).slice(0, 1000)); throw error;});
      else assert.equal(await history.getByText(locale === 'zh-CN' ? /外部停工待核实/ : /External work needs checking/).count(), 0);
      assert.deepEqual(errors, []);
      console.log(`${locale} uncertain=${uncertain} PASS`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
