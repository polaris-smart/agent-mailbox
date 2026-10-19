require('./browser_sandbox_guard.cjs');
// Real Human portal assets, synthetic API responses, no real mailbox or credentials.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const installHumanAccessStream = require('./human_access_stream_fixture.cjs');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
  try {
    for (const width of [320, 375, 414, 768, 907, 1200]) {
      const context = await browser.newContext({ viewport: { width, height: 680 } });
      const page = await context.newPage();
      page.setDefaultTimeout(6000);
      const errors = [];
      let enrollCalls = 0;
      let inviteCalls = 0;
      let projectRole = 'owner';
      let deviceActive = true;
      let deviceRevokes = 0;
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(installHumanAccessStream);
      await page.route('**/*', route => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.host !== 'mailbox.test') return route.abort();
        if (url.pathname.startsWith('/api/workbench/')) {
          const action = url.pathname.slice('/api/workbench/'.length);
          const bearer = request.headers().authorization;
          const json = (value, status = 200) => route.fulfill({ status, json: value });
          if (action === 'human/enroll' && request.method() === 'POST') {
            enrollCalls += 1;
            assert.equal(JSON.parse(request.postData()).invitation, 'ami1_synthetic');
            return json({ human_id: 'human_b', token: 'amh1_synthetic', recovery_code: 'amr1_synthetic' });
          }
          if (action === 'human-sessions' && request.method() === 'POST') {
            assert.equal(bearer, 'Bearer owner-synthetic');
            return json({ token: 'amh1_synthetic' });
          }
          if (bearer !== 'Bearer amh1_synthetic') return json({ error: { message: 'Unauthorized' } }, 401);
          if (action === 'human/self') return json({ human_id: 'human_b', display_name: 'Second Human' });
          if (action === 'human/spaces') return json({ spaces: [
            { id: 'space_b', name: 'Private space', kind: 'private' },
          ] });
          if (action === 'human/projects' && request.method() === 'GET') return json({ projects: [
            { id: 'project_b', name: '<img src=x onerror=alert(1)>', kind: 'private', role: projectRole },
          ] });
          if (action === 'human/projects/project_b/tasks') return json({ tasks: [
            { id: 'task_b', title: 'Own task', status: 'queued' },
          ] });
          if (action === 'human/projects/project_b/messages') return json({ messages: [
            { id: 'message_b', title: 'Group mail', body: 'Own message' },
          ] });
          if (action === 'human/projects/project_b/resources/manifest') return json({ resources: [] });
          if (action === 'human/projects/project_b/fleet/invites' && request.method() === 'POST') {
            inviteCalls += 1;
            assert.deepEqual(JSON.parse(request.postData()), {});
            return json({ version: 1, project_ids: ['project_b'], invite_secret: 'synthetic-once',
              base_url: 'https://127.0.0.1:49183', fingerprint: 'sha256:synthetic' });
          }
          if (action === 'human/projects/project_b/fleet/devices' && request.method() === 'GET')
            return json({ devices: deviceActive ? [
              { device_id: 'device_synthetic', name: 'Home laptop', paired_at: 1 },
            ] : [] });
          if (action === 'human/projects/project_b/fleet/devices/device_synthetic/revoke'
              && request.method() === 'POST') {
            deviceRevokes += 1;
            deviceActive = false;
            return json({ revoked: true });
          }
          if (action === 'human/logout') return json({ revoked: true });
          return json({ error: { message: 'Unexpected route' } }, 404);
        }
        const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
        return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
          contentType: name.endsWith('.js') ? 'text/javascript' :
            name.endsWith('.css') ? 'text/css' : 'text/html' });
      });
      await page.goto('http://mailbox.test/human');
      assert.equal(await page.locator('#owner-workbench').isVisible(), false);
      await page.getByLabel('邀请码').fill('ami1_synthetic');
      await page.getByRole('button', { name: '进入我的空间' }).click();
      const receipt = page.getByRole('dialog', { name: '保存恢复信息' });
      await receipt.waitFor();
      assert.equal(await receipt.locator('#receipt-code').textContent(), 'amr1_synthetic');
      await receipt.getByRole('button', { name: '我已保存' }).click();
      await page.waitForFunction(() => document.getElementById('receipt-code').textContent === '');
      assert.equal(await page.locator('#receipt-code').textContent(), '');
      await page.getByRole('button', { name: /<img src=x onerror=alert\(1\)>/ }).click();
      await page.getByText('Own task').waitFor();
      await page.getByText('Own message').waitFor();
      if (width === 375) {
        await page.getByRole('button', { name: '生成此项目设备邀请码' }).click();
        const pairing = page.getByRole('dialog', { name: '设备配对邀请' });
        await pairing.waitFor();
        assert.equal(JSON.parse(await pairing.locator('#fleet-invite-code').inputValue()).invite_secret,
          'synthetic-once');
        await pairing.getByRole('button', { name: '已妥善保存' }).click();
        assert.equal(await page.locator('#fleet-invite-code').inputValue(), '');
        assert.equal(inviteCalls, 1);
        await page.getByRole('button', { name: '查看已配对设备' }).click();
        await page.getByText('Home laptop').waitFor();
        await page.getByRole('button', { name: '撤销此项目权限' }).click();
        await page.getByRole('dialog', { name: '撤销设备项目访问？' })
          .getByRole('button', { name: '确认撤销' }).click();
        await page.getByText('此项目尚无已配对设备。').waitFor();
        assert.equal(deviceRevokes, 1);
        projectRole = 'viewer';
        await page.getByRole('button', { name: '刷新' }).click();
        await page.getByRole('button', { name: '生成此项目设备邀请码' }).waitFor({ state: 'detached' });
        assert.equal(await page.getByRole('button', { name: '生成此项目设备邀请码' }).count(), 0);
      }
      assert.equal(await page.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.HUMAN_PORTAL_SCREENSHOT_DIR && [320, 907, 1200].includes(width)) {
        fs.mkdirSync(process.env.HUMAN_PORTAL_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ path: path.join(process.env.HUMAN_PORTAL_SCREENSHOT_DIR,
          `human-portal-${width}.png`), fullPage: true });
      }
      await page.getByRole('button', { name: '退出此身份' }).click();
      await page.getByRole('heading', { name: '只进入属于你的工作区' }).waitFor();
      assert.equal(await page.getByText('Own task').count(), 0);
      assert.equal(await page.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
      await page.evaluate(() => sessionStorage.setItem('agent-mailbox.workbench.token', 'owner-synthetic'));
      await page.reload();
      assert.equal(await page.locator('#owner-workbench').isVisible(), true);
      await page.getByRole('button', { name: '使用本机管理者身份进入' }).click();
      await page.getByRole('heading', { name: '我的空间' }).waitFor();
      // Switching the same tab from a legacy owner session to an invited
      // Human must discard the older, broader bearer before showing content.
      await page.getByRole('button', { name: '退出此身份' }).click();
      await page.getByLabel('邀请码').fill('ami1_synthetic');
      await page.getByRole('button', { name: '进入我的空间' }).click();
      await page.getByRole('dialog', { name: '保存恢复信息' }).waitFor();
      assert.equal(await page.evaluate(() => sessionStorage.getItem('agent-mailbox.workbench.token')), null);
      assert.equal(await page.locator('#owner-workbench').isVisible(), false);
      assert.equal(await page.locator('#owner-entry').isVisible(), false);
      assert.equal(await page.locator('#invite-tools').isVisible(), false);
      if (width === 320) {
        await page.getByRole('dialog', { name: '保存恢复信息' })
          .getByRole('button', { name: '我已保存' }).click();
        await page.getByRole('button', { name: '退出此身份' }).click();
        await page.evaluate(() => {
          sessionStorage.setItem('agent-mailbox.workbench.token', 'owner-synthetic');
          const remove = Storage.prototype.removeItem;
          Storage.prototype.removeItem = function(key) {
            if (key === 'agent-mailbox.workbench.token') throw new Error('synthetic removal failure');
            return remove.call(this, key);
          };
        });
        const before = enrollCalls;
        await page.getByLabel('邀请码').fill('ami1_synthetic');
        await page.getByRole('button', { name: '进入我的空间' }).click();
        await page.getByText('无法清除本标签中的管理者会话').waitFor();
        assert.equal(enrollCalls, before);
        assert.equal(await page.evaluate(() => sessionStorage.getItem('agent-mailbox.workbench.token')),
          'owner-synthetic');
      }
      assert.deepEqual(errors, []);
      await context.close();
    }
    // A successful one-time claim must display its recovery code even if the
    // immediately following project read fails. The server cannot show it again.
    const outage = await browser.newContext({ viewport: { width: 375, height: 680 } });
    const interrupted = await outage.newPage();
    await interrupted.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== 'mailbox.test') return route.abort();
      if (url.pathname === '/api/workbench/human/enroll') return route.fulfill({ json: {
        human_id: 'human_b', token: 'amh1_after_claim', recovery_code: 'amr1_only_once',
      } });
      if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 503,
        json: { error: { message: 'Synthetic network outage' } } });
      const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
      return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
        contentType: name.endsWith('.js') ? 'text/javascript' :
          name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await interrupted.goto('http://mailbox.test/human');
    await interrupted.getByLabel('邀请码').fill('ami1_synthetic');
    await interrupted.getByRole('button', { name: '进入我的空间' }).click();
    const oneTime = interrupted.getByRole('dialog', { name: '保存恢复信息' });
    await oneTime.waitFor();
    assert.equal(await oneTime.locator('#receipt-code').textContent(), 'amr1_only_once');
    await outage.close();
    console.log('Human portal synthetic browser: 320/375/414/768/907/1200 passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
