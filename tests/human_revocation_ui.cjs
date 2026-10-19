require('./browser_sandbox_guard.cjs');
// Real Human assets; synthetic API and access stream; no production home or model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const installHumanAccessStream = require('./human_access_stream_fixture.cjs');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS ||
  path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 907, height: 680 } });
    const errors = [];
    let allowed = true;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(installHumanAccessStream);
    await page.addInitScript(() => sessionStorage.setItem('agent-mailbox.human.token', 'amh1_test'));
    await page.route('**/*', route => {
      const req = route.request();
      const url = new URL(req.url());
      if (url.host !== 'mailbox.test') return route.abort();
      const json = (value, status = 200) => route.fulfill({ status, json: value });
      if (!url.pathname.startsWith('/api/workbench/')) {
        const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
        return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
          contentType: name.endsWith('.js') ? 'text/javascript' :
            name.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      assert.equal(req.headers().authorization, 'Bearer amh1_test');
      const action = url.pathname.slice('/api/workbench/'.length);
      if (action === 'human/self') return json({ human_id: 'human_test', display_name: 'Test Human' });
      if (action === 'human/spaces') return json({ spaces: allowed ? [
        { id: 'space_shared', name: 'Shared', kind: 'shared', role: 'member' },
      ] : [] });
      if (action === 'human/projects') return json({ projects: allowed ? [
        { id: 'project_shared', name: 'Shared work', kind: 'shared',
          role: 'viewer', can_review: true, space_id: 'space_shared' },
      ] : [] });
      if (!allowed) return json({ error: { message: 'Access revoked' } }, 404);
      if (action === 'human/projects/project_shared/tasks') return json({ tasks: [
        { id: 'task_one', title: 'SYNTHETIC_PRIVATE_TASK', status: 'review' },
      ] });
      if (action === 'human/projects/project_shared/tasks/task_one/review') return json({
        title: 'SYNTHETIC_PRIVATE_REVIEW_TITLE',
        summary: 'SYNTHETIC_PRIVATE_REVIEW_SUMMARY',
        verification: { status: 'not_verified', notes: 'SYNTHETIC_PRIVATE_VERIFICATION' },
        diff: 'SYNTHETIC_PRIVATE_REVIEW_DIFF', files: [],
      });
      if (action === 'human/projects/project_shared/messages') return json({ messages: [] });
      if (action === 'human/projects/project_shared/resources/manifest') return json({ resources: [] });
      return json({ error: { message: 'Unexpected route' } }, 404);
    });
    await page.goto('http://mailbox.test/human');
    await page.getByRole('button', { name: /Shared work/ }).click();
    await page.getByText('SYNTHETIC_PRIVATE_TASK').waitFor();
    await page.getByRole('button', { name: '查看交付并验收' }).click();
    await page.locator('#review-dialog').waitFor({ state: 'visible' });
    assert.match(await page.locator('#review-dialog').innerText(), /SYNTHETIC_PRIVATE_VERIFICATION/);
    await page.waitForFunction(() => Boolean(window.__humanAccessController));

    await page.evaluate(() => {
      document.getElementById('fleet-invite-code').value = 'SYNTHETIC_PRIVATE_INVITE';
      document.getElementById('fleet-invite-receipt').showModal();
      document.getElementById('fleet-listener-state').textContent = 'SYNTHETIC_PRIVATE_ENDPOINT';
      document.getElementById('fleet-map-state').textContent = 'SYNTHETIC_PRIVATE_PATH';
      document.getElementById('human-forward-command').textContent = 'SYNTHETIC_PRIVATE_SSH';
      document.getElementById('fleet-map-project').add(new Option('SYNTHETIC_PRIVATE_PROJECT', 'private'));
      window.__closeHumanAccessStream();
    });
    await page.locator('#recheck-access').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#fleet-invite-receipt').evaluate(element => element.open), false);
    assert.equal(await page.locator('#fleet-invite-code').inputValue(), '');
    assert.doesNotMatch(await page.locator('#review-dialog').textContent(), /SYNTHETIC_PRIVATE_/);
    for (const selector of ['#fleet-listener-state', '#fleet-map-state',
      '#human-forward-command', '#fleet-map-project']) {
      assert.doesNotMatch(await page.locator(selector).textContent(), /SYNTHETIC_PRIVATE_/);
    }
    assert.doesNotMatch(await page.locator('body').textContent(), /SYNTHETIC_PRIVATE_/);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')),
      'amh1_test');
    await page.locator('#recheck-access').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: /Shared work/ }).click();
    await page.getByText('SYNTHETIC_PRIVATE_TASK').waitFor();
    await page.getByRole('button', { name: '查看交付并验收' }).click();
    await page.locator('#review-dialog').waitFor({ state: 'visible' });
    await page.waitForFunction(() => Boolean(window.__humanAccessController));

    allowed = false;
    await page.evaluate(() => window.__emitHumanAccessEvent({ access_changed: true }));
    await page.waitForFunction(() => !document.getElementById('projects').textContent.includes('Shared work'));
    assert.equal(await page.getByText('SYNTHETIC_PRIVATE_TASK').count(), 0);
    assert.equal(await page.locator('#detail-body').textContent(), '');
    assert.equal(await page.locator('#review-dialog').evaluate(element => element.open), false);
    assert.doesNotMatch(await page.locator('#review-dialog').textContent(), /SYNTHETIC_PRIVATE_/);
    assert.equal(await page.locator('#workspace').isVisible(), true);

    await page.evaluate(() => window.__emitHumanAccessEvent({ revoked: true }));
    await page.getByRole('heading', { name: '只进入属于你的工作区' }).waitFor();
    assert.equal(await page.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    assert.equal(await page.getByText('SYNTHETIC_PRIVATE_TASK').count(), 0);
    assert.deepEqual(errors, []);
    await page.close();
  } finally { await browser.close(); }
  console.log('Human access stream clears revoked project and session content');
})().catch(error => { console.error(error); process.exitCode = 1; });
