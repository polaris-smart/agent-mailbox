require('./browser_sandbox_guard.cjs');
// Real frontend assets with a synthetic owner response; no live mailbox or credentials.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}),
  });
  try {
    for (const locale of ['zh-CN', 'en-US']) {
      const context = await browser.newContext({ locale, viewport: { width: 900, height: 600 } });
      const page = await context.newPage();
      page.setDefaultTimeout(6000);
      const errors = [];
      const calls = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', async route => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname === '/api/workbench/bootstrap') {
          return route.fulfill({ json: {
            projects: [{ id: 'project', name: 'Synthetic project', path: '/synthetic/project' }],
            employees: [{ id: 'worker', name: 'Synthetic WorkBuddy', kind: 'workbuddy',
              node_id: 'local', lifecycle: 'active', project_id: 'project', project_ids: ['project'] }],
            node: { id: 'local', name: 'Synthetic device' }, devices: [], tasks: [],
            messages: [], resources: [], memories: [], runtime: { installed: false },
            version: '0.8.3-test', bootstrap_version: 1,
          } });
        }
        if (url.pathname === '/api/workbench/mailbox/receivers/native') {
          calls.push([url.searchParams.get('employee_id'), url.searchParams.get('project_id')]);
          return route.fulfill({ json: { host_kind: 'workbuddy', state: 'unsupported',
            component_available: false, binding_configured: false, route_configured: false,
            session_verified: false, delivery_verified: false,
            pending_alert_count: 2, oldest_pending_alert_at: '2026-10-18T00:00:00+00:00' } });
        }
        if (url.pathname.startsWith('/api/')) {
          assert.equal(request.method(), 'GET');
          return route.fulfill({ json: { events: [], notifications: [], capabilities: [], revision: 0 } });
        }
        const name = path.basename(url.pathname) || 'index.html';
        return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
          contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
      });
      await page.addInitScript(() => {
        const original = window.fetch.bind(window);
        window.fetch = (url, options = {}) => url === '/api/workbench/changes'
          ? Promise.resolve(new Response(new ReadableStream({ start(controller) {
            controller.enqueue(new TextEncoder().encode('{"revision":0}\n'));
            options.signal?.addEventListener('abort', () => controller.close(), { once: true });
          } }), { headers: { 'Content-Type': 'application/x-ndjson' } }))
          : original(url, options);
      });
      await page.goto('http://mailbox.test/#token=synthetic');
      await page.locator('[data-view="mailboxes"]').first().click();
      await page.getByRole('button', { name: locale === 'zh-CN' ? '接收状态' : 'Reception status', exact: true }).click();
      const status = page.getByRole('dialog').locator('#native-receiver-status');
      const expected = locale === 'zh-CN'
        ? '当前系统或宿主暂不支持原会话自动接收邮件提醒'
        : 'This system or host does not yet support automatic mail alerts in an existing session';
      await status.getByText(expected, { exact: false }).waitFor();
      await page.getByRole('dialog').locator('#native-receiver-backlog').getByText(
        locale === 'zh-CN' ? '2 条待投递提醒' : '2 pending alerts', { exact: false }).waitFor();
      assert.deepEqual(calls, [['worker', 'project']]);
      await page.getByRole('dialog').getByRole('button', { name: locale === 'zh-CN' ? '关闭' : 'Close', exact: true }).first().click();
      await page.locator('[data-view="tasks"]').first().click();
      await page.locator('[data-action="new-task"]').first().click();
      const taskDialog = page.getByRole('dialog');
      await taskDialog.locator('#task-receiver-status').getByText(
        locale === 'zh-CN' ? '原会话自动提醒尚不可用' : 'Automatic alerts to this employee',
        { exact: false },
      ).waitFor();
      assert.equal(await taskDialog.getByRole('button', { name: locale === 'zh-CN' ? '派发任务' : 'Assign task' }).isEnabled(), true);
      assert.deepEqual(calls, [['worker', 'project'], ['worker', 'project']]);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log('WorkBuddy unsupported original-session UI: zh-CN/en-US passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
