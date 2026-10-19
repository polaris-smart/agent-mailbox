require('./browser_sandbox_guard.cjs');
// Same-browser-profile Human switch with real assets and synthetic identities.
// An opaque epoch is shared between tabs; bearer tokens remain tab-local.
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
    const context = await browser.newContext();
    const errors = [];
    let signalForwardRequest;
    let releaseForwardStatus;
    let signalForwardComplete;
    const forwardRequested = new Promise(resolve => { signalForwardRequest = resolve; });
    const forwardCompleted = new Promise(resolve => { signalForwardComplete = resolve; });
    await context.addInitScript(installHumanAccessStream);
    await context.route('**/*', async route => {
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
      const action = url.pathname.slice('/api/workbench/'.length);
      if (action === 'human-forward/status') {
        signalForwardRequest();
        await new Promise(resolve => { releaseForwardStatus = resolve; });
        await json({ enabled: false });
        signalForwardComplete();
        return;
      }
      if (action === 'human/enroll') {
        const letter = JSON.parse(req.postData()).invitation.at(-1);
        assert.ok(['a', 'b'].includes(letter));
        return json({ human_id: 'human_' + letter, token: 'amh1_' + letter,
          recovery_code: 'amr1_' + letter });
      }
      const bearer = req.headers().authorization || '';
      const letter = bearer.startsWith('Bearer amh1_') ? bearer.slice('Bearer amh1_'.length) : '';
      if (!['a', 'b'].includes(letter)) return json({ error: { message: 'Unauthorized' } }, 401);
      if (action === 'human/self') return json({ human_id: 'human_' + letter,
        display_name: 'Human ' + letter.toUpperCase() });
      if (action === 'human/spaces') return json({ spaces: [
        { id: 'space_' + letter, name: 'Space ' + letter.toUpperCase(), kind: 'private' },
      ] });
      if (action === 'human/projects') return json({ projects: [
        { id: 'project_' + letter, name: 'Project ' + letter.toUpperCase(),
          kind: 'private', role: 'owner', space_id: 'space_' + letter },
      ] });
      if (action === 'human/projects/project_' + letter + '/tasks') return json({ tasks: [
        { id: 'task_' + letter, title: 'PRIVATE_' + letter.toUpperCase(), status: 'queued' },
      ] });
      if (action === 'human/projects/project_' + letter + '/messages') return json({ messages: [] });
      if (action === 'human/projects/project_' + letter + '/resources/manifest')
        return json({ resources: [] });
      return json({ error: { message: 'Unexpected route' } }, 404);
    });
    const pageA = await context.newPage();
    const pageB = await context.newPage();
    for (const page of [pageA, pageB]) page.on('pageerror', error => errors.push(error.message));
    await pageA.goto('http://mailbox.test/human');
    await pageA.getByLabel('邀请码').fill('ami1_a');
    await pageA.getByRole('button', { name: '进入我的空间' }).click();
    await pageA.getByRole('dialog', { name: '保存恢复信息' })
      .getByRole('button', { name: '我已保存' }).click();
    await pageA.getByRole('button', { name: /Project A/ }).click();
    await pageA.getByText('PRIVATE_A').waitFor();
    const oldEpoch = await pageA.evaluate(() => sessionStorage.getItem('agent-mailbox.human.tab-epoch'));
    assert.ok(oldEpoch);

    // An owner tab can have both credentials. Hold the owner-only status
    // response while another Human takes over the same browser profile.
    await pageA.evaluate(() => sessionStorage.setItem('agent-mailbox.workbench.token', 'owner-test'));
    await pageA.getByRole('button', { name: '刷新' }).click();
    await forwardRequested;

    await pageB.goto('http://mailbox.test/human');
    await pageB.getByLabel('邀请码').fill('ami1_b');
    await pageB.getByRole('button', { name: '进入我的空间' }).click();
    await pageB.getByRole('dialog', { name: '保存恢复信息' })
      .getByRole('button', { name: '我已保存' }).click();
    await pageB.getByRole('button', { name: /Project B/ }).click();
    await pageB.getByText('PRIVATE_B').waitFor();
    await pageA.getByRole('heading', { name: '只进入属于你的工作区' }).waitFor();
    assert.equal(await pageA.getByText('PRIVATE_A').count(), 0);
    assert.equal(await pageA.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    assert.equal(await pageA.evaluate(() => sessionStorage.getItem('agent-mailbox.workbench.token')),
      null, 'another Human taking over the browser profile must clear the old owner bearer');
    releaseForwardStatus();
    await forwardCompleted;
    await pageA.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await pageA.locator('#projects').locator('button').count(), 0);
    assert.equal(await pageA.getByText('PRIVATE_A').count(), 0);
    assert.equal(await pageB.evaluate(() => localStorage.getItem('agent-mailbox.human.profile-epoch')
      .includes('amh1_')), false);

    // A suspended tab that missed the cross-tab event still fails closed on load.
    await pageA.evaluate(epoch => {
      sessionStorage.setItem('agent-mailbox.human.token', 'amh1_a');
      sessionStorage.setItem('agent-mailbox.human.tab-epoch', epoch);
      sessionStorage.setItem('agent-mailbox.workbench.token', 'stale-owner-test');
    }, oldEpoch);
    await pageA.reload();
    await pageA.getByRole('heading', { name: '只进入属于你的工作区' }).waitFor();
    assert.equal(await pageA.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    assert.equal(await pageA.evaluate(() => sessionStorage.getItem('agent-mailbox.workbench.token')),
      null, 'a restored stale Human tab must also clear its old owner bearer');
    assert.equal(await pageA.getByText('PRIVATE_A').count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); }
  console.log('Human same-profile switch clears old tab and stale restored token');
})().catch(error => { console.error(error); process.exitCode = 1; });
