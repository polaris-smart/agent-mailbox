require('./browser_sandbox_guard.cjs');
// The same Human page must manage SSH access locally yet hide it from remote Humans.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({viewport: {width: 375, height: 667}});
    const page = await context.newPage();
    const errors = [];
    let enabled = false;
    let fleetEnabled = false;
    let fleetStartAddress = '';
    let fleetJoined = false;
    let fleetJoinInvite = null;
    let fleetMapped = null;
    let fleetEmployee = null;
    let discoveryFails = false;
    let fleetBaseUrl = 'https://192.168.1.10:49183';
    const auth = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      sessionStorage.setItem('agent-mailbox.workbench.token', 'owner-synthetic');
      const original = window.fetch.bind(window);
      window.fetch = (url, opts = {}) => url === '/api/workbench/human/changes'
        ? Promise.resolve(new Response(new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode('{"ready":true}\n')); opts.signal?.addEventListener('abort', () => c.close(), {once: true});}}), {headers: {'Content-Type': 'application/x-ndjson'}}))
        : original(url, opts);
    });
    page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== 'mailbox.test') return route.abort();
      if (url.pathname === '/api/workbench/human-sessions') return route.fulfill({json: {token: 'amh1_owner', human_id: 'human_owner'}});
      if (url.pathname === '/api/workbench/human/self') return route.fulfill({json: {human_id: 'human_owner', display_name: 'Owner'}});
      if (url.pathname === '/api/workbench/human/spaces') return route.fulfill({json: {spaces: []}});
      if (url.pathname === '/api/workbench/human/projects') return route.fulfill({json: {projects: []}});
      if (url.pathname.startsWith('/api/workbench/human-forward/')) {
        auth.push(route.request().headers()['authorization']);
        if (url.pathname.endsWith('/start')) enabled = true;
        if (url.pathname.endsWith('/stop')) enabled = false;
        return route.fulfill({json: url.pathname.endsWith('/status') ? {enabled, port: enabled ? 49182 : null} : url.pathname.endsWith('/stop') ? {stopped: true} : {enabled, port: 49182}});
      }
      if (url.pathname.startsWith('/api/workbench/fleet/')) {
        auth.push(route.request().headers()['authorization']);
        if (url.pathname.endsWith('/discover')) return route.fulfill({json: {
          trusted: false, devices: [{base_url: 'https://192.168.1.12:49001', verified: false}]
        }});
        if (url.pathname.endsWith('/join')) {
          fleetJoinInvite = JSON.parse(route.request().postData()).invite;
          fleetJoined = true;
          return route.fulfill({json: {remote: {paired: true}}});
        }
        if (url.pathname.endsWith('/map')) {
          fleetMapped = JSON.parse(route.request().postData());
          return route.fulfill({json: fleetMapped});
        }
        if (url.pathname.endsWith('/available-employees')) {
          if (discoveryFails) return route.fulfill({status: 503, json: {error: {message: 'synthetic unavailable'}}});
          return route.fulfill({json: {employees: [{kind: 'codex', name: 'Synthetic CLI'}]}});
        }
        if (url.pathname.endsWith('/employee')) {
          fleetEmployee = JSON.parse(route.request().postData());
          return route.fulfill({json: {id: 'employee_test', ...fleetEmployee}});
        }
        if (url.pathname.endsWith('/relocate')) {
          fleetBaseUrl = JSON.parse(route.request().postData()).base_url;
          return route.fulfill({json: {remote: {paired: true, base_url: fleetBaseUrl}}});
        }
        if (url.pathname.endsWith('/leave')) {
          fleetJoined = false;
          return route.fulfill({json: {disconnected: true}});
        }
        if (url.pathname.endsWith('/start')) {
          fleetStartAddress = JSON.parse(route.request().postData()).address;
          fleetEnabled = true;
        }
        if (url.pathname.endsWith('/stop')) fleetEnabled = false;
        return route.fulfill({json: url.pathname.endsWith('/connection')
          ? {configured: fleetJoined, saved_pairing: fleetJoined, base_url: fleetJoined ? fleetBaseUrl : null,
            projects: fleetJoined ? [{id: 'project_test', name: 'Shared work'}] : [],
            mappings: fleetJoined && fleetMapped ? {[fleetMapped.project_id]: fleetMapped.path} : {}}
          : url.pathname.endsWith('/status')
          ? {enabled: fleetEnabled, base_url: fleetEnabled ? 'https://192.168.1.10:49183' : null,
            lan_advertised: fleetEnabled, lan_error: null}
          : url.pathname.endsWith('/stop') ? {stopped: true} : {base_url: 'https://192.168.1.10:49183'}});
      }
      if (url.pathname.startsWith('/api/')) return route.fulfill({json: {}});
      const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
      return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://mailbox.test/human');
    await page.getByRole('button', {name: '使用本机管理者身份进入'}).click();
    await page.locator('#human-forward-tools summary').click();
    await page.getByRole('button', {name: '启用 SSH 转发入口'}).click();
    await page.getByText(/127\.0\.0\.1:49182 user@home-host/).waitFor();
    await page.getByRole('button', {name: '停止 Human 远程入口'}).click();
    await page.getByRole('button', {name: '启用 SSH 转发入口'}).waitFor();
    await page.locator('#fleet-listener-tools summary').click();
    await page.getByLabel('监听地址').fill('192.168.1.10');
    await page.getByRole('button', {name: '启用设备监听'}).click();
    await page.getByText(/设备监听已启用：https:\/\/192\.168\.1\.10:49183/).waitFor();
    assert.match(await page.locator('#fleet-ssh-guide').textContent(),
      /ssh -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127\.0\.0\.1:49181:192\.168\.1\.10:49183 USER@SSH_HOST/);
    assert.equal(fleetStartAddress, '192.168.1.10');
    assert.equal(await page.getByLabel('监听地址').isDisabled(), true);
    await page.getByRole('button', {name: '停止设备监听'}).click();
    await page.getByText('设备监听未启用。').waitFor();
    assert.equal(await page.locator('#fleet-ssh-guide').isHidden(), true);
    assert.equal(await page.getByLabel('监听地址').isEnabled(), true);
    await page.locator('#fleet-join-tools summary').click();
    await page.getByRole('button', {name: '查找同网主控'}).click();
    await page.getByText(/使用发现的地址 https:\/\/192\.168\.1\.12:49001/).waitFor();
    await page.locator('#fleet-join-invite').fill(JSON.stringify({
      base_url: 'https://192.168.1.10:49183', fingerprint: 'sha256:synthetic',
      invite_id: 'test', invite_secret: 'synthetic-secret'
    }));
    await page.getByRole('button', {name: /使用发现的地址/}).click();
    assert.equal(JSON.parse(await page.locator('#fleet-join-invite').inputValue()).base_url,
      'https://192.168.1.12:49001');
    assert.equal(fleetJoined, false, 'LAN suggestion must not join automatically');
    await page.locator('#fleet-join-invite').fill('{"base_url":"https://192.168.1.10:49183","marker":"ui-test"}');
    await page.getByRole('button', {name: '验证并加入主控'}).click();
    await page.getByText(/此设备已保存主控配对/).waitFor();
    assert.equal(fleetJoinInvite.marker, 'ui-test');
    assert.equal(await page.locator('#fleet-join-invite').inputValue(), '');
    assert.equal(await page.locator('#fleet-join-submit').isHidden(), true);
    await page.locator('#fleet-map-path').fill('/tmp/synthetic-project');
    await page.getByRole('button', {name: '使用这个目录'}).click();
    await page.getByText(/已选目录：\/tmp\/synthetic-project/).waitFor();
    assert.deepEqual(fleetMapped, {project_id: 'project_test', path: '/tmp/synthetic-project'});
    await page.locator('#fleet-employee-name').fill('Fixture worker');
    await page.getByRole('button', {name: '加入此项目'}).click();
    await page.getByText(/员工已加入主控项目/).waitFor();
    assert.deepEqual(fleetEmployee, {project_id: 'project_test', name: 'Fixture worker', kind: 'codex'});
    assert.equal(await page.locator('#fleet-employee-name').inputValue(), '');
    discoveryFails = true;
    await page.locator('#fleet-map-project').dispatchEvent('change');
    await page.getByText('本机工具检查失败：synthetic unavailable').waitFor();
    assert.equal(await page.locator('#fleet-join-tools').isHidden(), false);
    assert.equal(await page.locator('#fleet-employee-submit').isDisabled(), true);
    await page.locator('#fleet-relocate-url').fill('https://localhost:49183');
    await page.getByRole('button', {name: '核验并更新地址'}).click();
    await page.getByText(/此设备已保存主控配对：https:\/\/localhost:49183/).waitFor();
    assert.equal(fleetBaseUrl, 'https://localhost:49183');
    await page.getByRole('button', {name: '断开此设备'}).click();
    await page.locator('#fleet-leave-confirm').getByRole('button', {name: '取消'}).click();
    assert.equal(fleetJoined, true);
    await page.getByRole('button', {name: '断开此设备'}).click();
    await page.locator('#fleet-leave-confirm').getByRole('button', {name: '确认断开'}).click();
    await page.getByText('此设备尚未加入主控。').waitFor();
    assert.equal(fleetJoined, false);
    assert.equal(await page.locator('#fleet-join-submit').isHidden(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.ok(auth.length >= 5 && auth.every(value => value === 'Bearer owner-synthetic'));
    assert.deepEqual(errors, []);
    console.log('local owner PASS');

    const remote = await browser.newContext({viewport: {width: 375, height: 667}});
    const remotePage = await remote.newPage();
    await remotePage.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) return route.fulfill({status: 404, json: {error: {code: 'NOT_FOUND'}}});
      const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
      return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await remotePage.goto('http://mailbox.test/human');
    assert.equal(await remotePage.locator('#human-forward-tools').isHidden(), true);
    assert.equal(await remotePage.locator('#fleet-listener-tools').isHidden(), true);
    assert.equal(await remotePage.locator('#fleet-join-tools').isHidden(), true);
    console.log('remote Human PASS');
    await remote.close();
    await context.close();
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
