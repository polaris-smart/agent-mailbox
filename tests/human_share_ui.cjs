require('./browser_sandbox_guard.cjs');
// Real Human portal assets with a synthetic, stateful sharing API.
// No real home, credentials, messages or model calls are used.
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
    for (const width of [375, 1200]) {
      const context = await browser.newContext({ viewport: { width, height: 760 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(installHumanAccessStream);
      await page.addInitScript(() => sessionStorage.setItem(
        'agent-mailbox.workbench.token', 'owner-synthetic'));
      const project = { id: 'project_owner', name: 'Family project',
        kind: 'private', role: 'owner', space_id: 'space_private' };
      const spaces = [{ id: 'space_private', name: 'My space', kind: 'private', role: 'owner' }];
      const members = [{ id: 'human_owner', display_name: 'Owner', role: 'owner' }];
      const grants = [{ human_id: 'human_owner', display_name: 'Owner', role: 'owner' }];
      let invitations = 0;
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
        const action = url.pathname.slice('/api/workbench/'.length);
        const bearer = req.headers().authorization;
        const body = req.postData() ? JSON.parse(req.postData()) : {};
        if (action === 'human-sessions' && req.method() === 'POST') {
          assert.equal(bearer, 'Bearer owner-synthetic');
          return json({ token: 'amh1_owner' });
        }
        if (action === 'humans' && req.method() === 'POST') {
          assert.equal(bearer, 'Bearer owner-synthetic');
          assert.equal(body.display_name, 'Guest');
          return json({ human_id: 'human_guest', private_space_id: 'space_guest' });
        }
        if (action === 'human-invitations' && req.method() === 'POST') {
          assert.equal(bearer, 'Bearer owner-synthetic');
          assert.equal(body.human_id, 'human_guest');
          invitations += 1;
          return json({ human_id: 'human_guest', invitation: 'ami1_synthetic',
            expires_at: '2026-10-17T00:00:00+00:00' });
        }
        if (bearer !== 'Bearer amh1_owner') return json({ error: { message: 'Unauthorized' } }, 401);
        if (action === 'human/self') return json({ human_id: 'human_owner', display_name: 'Owner' });
        if (action === 'human/spaces' && req.method() === 'GET') return json({ spaces });
        if (action === 'human/spaces' && req.method() === 'POST') {
          assert.equal(body.name, 'Family space');
          spaces.push({ id: 'space_shared', name: body.name, kind: 'shared', role: 'owner' });
          return json({ space_id: 'space_shared' });
        }
        if (action === 'human/projects') return json({ projects: [project] });
        if (action === 'human/projects/project_owner/tasks') return json({ tasks: [] });
        if (action === 'human/projects/project_owner/messages') return json({ messages: [] });
        if (action === 'human/projects/project_owner/resources/manifest') return json({ resources: [] });
        if (action === 'human/projects/project_owner/share' && req.method() === 'POST') {
          assert.equal(body.space_id, 'space_shared');
          project.kind = 'shared';
          project.space_id = 'space_shared';
          return json({ project_id: project.id, space_id: project.space_id });
        }
        if (action === 'human/spaces/space_shared/members' && req.method() === 'GET') {
          return json({ members });
        }
        if (action === 'human/spaces/space_shared/members' && req.method() === 'POST') {
          assert.equal(body.human_id, 'human_guest');
          members.push({ id: 'human_guest', display_name: '<img src=x onerror=alert(1)>',
            role: 'member' });
          return json({ space_id: 'space_shared', human_id: 'human_guest' });
        }
        if (action === 'human/spaces/space_shared/members/human_guest' &&
            req.method() === 'DELETE') {
          members.pop();
          if (grants.at(-1)?.human_id === 'human_guest') grants.pop();
          return json({ removed: true });
        }
        if (action === 'human/projects/project_owner/grants' && req.method() === 'GET') {
          return json({ grants });
        }
        if (action === 'human/projects/project_owner/grants' && req.method() === 'POST') {
          assert.deepEqual(body, { human_id: 'human_guest', role: 'viewer' });
          grants.push({ human_id: 'human_guest', display_name: members[1].display_name,
            role: 'viewer' });
          return json({ project_id: project.id, ...body });
        }
        if (action === 'human/projects/project_owner/grants/human_guest' &&
            req.method() === 'DELETE') {
          grants.pop();
          return json({ revoked: true });
        }
        return json({ error: { message: 'Unexpected route: ' + action } }, 404);
      });
      await page.goto('http://mailbox.test/human');
      await page.getByRole('button', { name: '使用本机管理者身份进入' }).click();
      await page.getByRole('heading', { name: '我的空间' }).waitFor();

      await page.locator('#invite-tools summary').click();
      await page.getByLabel('成员名称').fill('Guest');
      await page.getByRole('button', { name: '创建身份' }).click();
      await page.getByText('新成员 Human ID：human_guest').waitFor();
      assert.equal(await page.locator('#invite-human-id').inputValue(), 'human_guest');
      await page.getByRole('button', { name: '生成邀请码' }).click();
      const invite = page.getByRole('dialog', { name: '交给受邀成员' });
      await invite.waitFor();
      assert.equal(await invite.locator('#invitation-code').textContent(), 'ami1_synthetic');
      await invite.getByRole('button', { name: '已妥善保存' }).click();
      await page.waitForFunction(() => document.getElementById('invitation-code').textContent === '');
      assert.equal(await page.locator('#invitation-code').textContent(), '');
      assert.equal(invitations, 1);

      await page.getByLabel('新建共享空间').fill('Family space');
      await page.locator('#create-space button').click();
      await page.getByText('Family space').waitFor();
      await page.getByRole('button', { name: /Family project/ }).click();
      await page.getByRole('heading', { name: '共享此项目' }).waitFor();
      await page.getByLabel('目标共享空间').selectOption('space_shared');
      await page.getByRole('button', { name: '放入共享空间' }).click();
      await page.getByRole('heading', { name: '项目访问' }).waitFor();
      assert.deepEqual(grants.map(item => item.human_id), ['human_owner']);
      await page.getByLabel('加入共享空间的 Human ID').fill('human_guest');
      await page.getByRole('button', { name: '加入空间' }).click();
      assert.deepEqual(grants.map(item => item.human_id), ['human_owner']);
      await page.locator('.grant-member select').first().selectOption('human_guest');
      await page.getByLabel('项目权限').selectOption('viewer');
      await page.getByRole('button', { name: '授予或更新权限' }).click();
      await page.getByRole('button', { name: /撤销 .* 的项目访问/ }).waitFor();
      assert.deepEqual(grants.map(item => item.human_id), ['human_owner', 'human_guest']);
      assert.equal(await page.locator('img').count(), 0);
      if (process.env.HUMAN_SHARE_SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.HUMAN_SHARE_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.HUMAN_SHARE_SCREENSHOT_DIR,
          `human-share-${width}.png`), fullPage: true });
      }
      await page.getByRole('button', { name: /撤销 .* 的项目访问/ }).click();
      await page.waitForFunction(() => document.querySelectorAll('.grant-list button').length === 0);
      assert.deepEqual(grants.map(item => item.human_id), ['human_owner']);
      await page.locator('.grant-member select').first().selectOption('human_guest');
      await page.getByRole('button', { name: '授予或更新权限' }).click();
      await page.getByRole('button', { name: /将 .* 移出共享空间/ }).click();
      const confirm = page.getByRole('dialog', { name: '移出共享空间？' });
      await confirm.waitFor();
      if (process.env.HUMAN_SHARE_SCREENSHOT_DIR && width === 375) {
        await page.screenshot({ path: path.join(process.env.HUMAN_SHARE_SCREENSHOT_DIR,
          'human-share-confirm-375.png'), fullPage: true });
      }
      await confirm.getByRole('button', { name: '取消' }).click();
      assert.equal(members.length, 2);
      await page.getByRole('button', { name: /将 .* 移出共享空间/ }).click();
      await confirm.getByRole('button', { name: '确认移出' }).click();
      await page.waitForFunction(() => document.querySelectorAll('.space-member-list button').length === 0);
      assert.deepEqual(members.map(item => item.id), ['human_owner']);
      assert.deepEqual(grants.map(item => item.human_id), ['human_owner']);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log('Human share UI: invite → space → grant → revoke → member removal passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
