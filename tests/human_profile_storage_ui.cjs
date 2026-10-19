require('./browser_sandbox_guard.cjs');
// A shared browser profile must fail closed when it cannot coordinate Human identities.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS ||
  path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
  try {
    let enrollCalls = 0;
    let selfCalls = 0;
    const context = await browser.newContext();
    await context.addInitScript(() => {
      Object.defineProperty(window, 'localStorage', { configurable: true,
        get() { throw new DOMException('blocked', 'SecurityError'); } });
      Object.defineProperty(window, 'BroadcastChannel', { configurable: true, value: undefined });
    });
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== 'mailbox.test') return route.abort();
      if (!url.pathname.startsWith('/api/workbench/')) {
        const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
        return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
          contentType: name.endsWith('.js') ? 'text/javascript' :
            name.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      const action = url.pathname.slice('/api/workbench/'.length);
      if (action === 'human/enroll') {
        enrollCalls++;
        return route.fulfill({ json: { human_id: 'human_a', token: 'amh1_a',
          recovery_code: 'amr1_a' } });
      }
      if (action === 'human/self') selfCalls++;
      return route.fulfill({ status: 404, json: { error: { message: 'Unexpected request' } } });
    });

    const fresh = await context.newPage();
    await fresh.goto('http://mailbox.test/human');
    await fresh.getByLabel('邀请码').fill('ami1_a');
    await fresh.getByRole('button', { name: '进入我的空间' }).click();
    assert.equal(enrollCalls, 0, 'unsafe browser must not consume an invitation');
    assert.equal(await fresh.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    assert.match(await fresh.locator('#notice').innerText(), /禁止站点共享存储/);
    await fresh.close();

    const restored = await context.newPage();
    await restored.addInitScript(() => {
      sessionStorage.setItem('agent-mailbox.human.token', 'amh1_old');
      sessionStorage.setItem('agent-mailbox.human.tab-epoch', 'old');
    });
    await restored.goto('http://mailbox.test/human');
    assert.equal(await restored.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    assert.equal(selfCalls, 0, 'old Human token must not be read in an uncoordinated profile');
    assert.match(await restored.locator('#notice').innerText(), /禁止站点共享存储/);
    await context.close();

    // A transient failure to inspect the owner token must not consume a Human
    // invitation while that old owner credential could remain in this tab.
    const ownerContext = await browser.newContext();
    await ownerContext.addInitScript(() => {
      sessionStorage.setItem('agent-mailbox.workbench.token', 'owner-test');
      const originalGetItem = Storage.prototype.getItem;
      Storage.prototype.getItem = function(key) {
        if (key === 'agent-mailbox.workbench.token')
          throw new DOMException('temporarily blocked', 'SecurityError');
        return originalGetItem.call(this, key);
      };
    });
    await ownerContext.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== 'mailbox.test') return route.abort();
      if (!url.pathname.startsWith('/api/workbench/')) {
        const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
        return route.fulfill({ body: fs.readFileSync(path.join(assets, name)),
          contentType: name.endsWith('.js') ? 'text/javascript' :
            name.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      if (url.pathname === '/api/workbench/human/enroll') enrollCalls++;
      return route.fulfill({ status: 404, json: { error: { message: 'Unexpected request' } } });
    });
    const uncertainOwner = await ownerContext.newPage();
    await uncertainOwner.goto('http://mailbox.test/human');
    await uncertainOwner.getByLabel('邀请码').fill('ami1_b');
    await uncertainOwner.getByRole('button', { name: '进入我的空间' }).click();
    assert.equal(enrollCalls, 0, 'owner storage read failure must not consume an invitation');
    assert.match(await uncertainOwner.locator('#notice').innerText(), /请允许本站点使用标签页存储/);
    // Even after removing the known owner token, a failed read cannot prove
    // that the old credential is absent when the browser later restores storage.
    await uncertainOwner.evaluate(() => sessionStorage.removeItem('agent-mailbox.workbench.token'));
    await uncertainOwner.getByLabel('邀请码').fill('ami1_c');
    await uncertainOwner.getByRole('button', { name: '进入我的空间' }).click();
    assert.equal(enrollCalls, 0, 'unreadable owner storage must stay fail-closed without a known token');
    assert.match(await uncertainOwner.locator('#notice').innerText(), /请允许本站点使用标签页存储/);
    await ownerContext.close();
  } finally { await browser.close(); }
  console.log('Human profile storage unavailable: invitation preserved and stale token cleared');
})().catch(error => { console.error(error); process.exitCode = 1; });
