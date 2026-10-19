require('./browser_sandbox_guard.cjs');
// Real Human HTML/JS, real loopback API, separate browser profiles, synthetic home.
const assert = require('node:assert/strict');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.MAILBOX_PYTHON || path.join(root, '.venv/bin/python');

async function fixture() {
  const child = spawn(python, [path.join(__dirname, 'human_real_http_fixture.py')], {
    cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, 'src') },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let errors = '';
  child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-4000); });
  const lines = readline.createInterface({ input: child.stdout });
  const ready = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('fixture startup timed out: ' + errors)), 20000);
    lines.once('line', line => {
      clearTimeout(timeout);
      try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
    });
    child.once('exit', code => { clearTimeout(timeout); reject(new Error('fixture exited ' + code + ': ' + errors)); });
  });
  return { child, lines, ready, errors: () => errors };
}

(async () => {
  const { child, lines, ready, errors } = await fixture();
  let browser;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    const ownerContext = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const guestContext = await browser.newContext({ viewport: { width: 375, height: 760 } });
    const owner = await ownerContext.newPage();
    const guest = await guestContext.newPage();
    const pageErrors = [];
    for (const page of [owner, guest]) page.on('pageerror', error => pageErrors.push(error.message));

    await owner.addInitScript(value => sessionStorage.setItem('agent-mailbox.workbench.token', value),
      ready.owner_token);
    await owner.goto(ready.endpoint + '/human');
    await owner.locator('#owner-entry').click();
    await owner.locator('#workspace').waitFor({ state: 'visible' });
    assert.match(await owner.locator('#projects').innerText(), /OWNER_PRIVATE_MARKER/);
    assert.match(await owner.locator('#projects').innerText(), /SHARED_PROJECT_MARKER/);
    assert.doesNotMatch(await owner.locator('#projects').innerText(), /GUEST_PRIVATE_MARKER/);
    const ownerBearer = await owner.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token'));
    assert.ok(ownerBearer?.startsWith('amh1_'));

    await guest.goto(ready.human_endpoint + '/human');
    await guest.locator('#invitation').fill(ready.guest_invitation);
    await guest.locator('#enroll-form button[type=submit]').click();
    await guest.locator('#recovery-receipt').waitFor({ state: 'visible' });
    const recoveryCode = await guest.locator('#receipt-code').innerText();
    assert.match(recoveryCode, /^amr1_/);
    await guest.locator('#receipt-close').click();
    await guest.locator('#workspace').waitFor({ state: 'visible' });
    const guestProjects = await guest.locator('#projects').innerText();
    assert.match(guestProjects, /GUEST_PRIVATE_MARKER/);
    assert.match(guestProjects, /SHARED_PROJECT_MARKER/);
    assert.doesNotMatch(guestProjects, /OWNER_PRIVATE_MARKER/);
    await guest.locator('#projects [data-project-id="' + ready.shared + '"]').click();
    await guest.getByText('SHARED_TASK_MARKER').waitFor();
    const taskAdded = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('shared task fixture timed out')), 5000);
      lines.once('line', line => {
        clearTimeout(timeout);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
    });
    child.stdin.write('add-shared-task\n');
    assert.deepEqual(await taskAdded, { shared_task_added: true });
    await guest.getByText('NEW_SHARED_TASK_MARKER').waitFor({ timeout: 5000 });
    const messageAdded = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('shared message fixture timed out')), 5000);
      lines.once('line', line => {
        clearTimeout(timeout);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
    });
    child.stdin.write('add-shared-message\n');
    assert.deepEqual(await messageAdded, { shared_message_added: true });
    await guest.getByText('NEW_SHARED_MESSAGE_MARKER').waitFor({ timeout: 5000 });
    const resourceAdded = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('shared resource fixture timed out')), 5000);
      lines.once('line', line => {
        clearTimeout(timeout);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
    });
    child.stdin.write('add-shared-resource\n');
    const { shared_resource_id: resourceId } = await resourceAdded;
    assert.ok(resourceId.startsWith('resource_'));
    await guest.getByRole('button', { name: '读取资料 ' + resourceId }).click({ timeout: 5000 });
    await guest.getByText('NEW_SHARED_RESOURCE_MARKER').waitFor({ timeout: 5000 });
    const guestBearer = await guest.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token'));
    assert.ok(guestBearer?.startsWith('amh1_'));

    const legacy = await fetch(ready.human_endpoint + '/api/workbench/bootstrap', {
      headers: { Authorization: 'Bearer ' + guestBearer },
    });
    assert.equal(legacy.status, 404);
    const ownerViaForward = await fetch(ready.human_endpoint + '/api/workbench/human/projects', {
      headers: { Authorization: 'Bearer ' + ready.owner_token },
    });
    assert.equal(ownerViaForward.status, 401);
    assert.equal((await fetch(ready.human_endpoint + '/workbench-assets/workbench.js')).status, 404);
    // Revoke while the visitor is away, then return through browser history.
    // A restored page must not show the old project's cached DOM.
    await guest.goto('about:blank');
    const revoked = await fetch(ready.endpoint + '/api/workbench/human/projects/' +
      ready.shared + '/grants/' + ready.guest_id, {
      method: 'DELETE', headers: { Authorization: 'Bearer ' + ownerBearer },
    });
    assert.equal(revoked.status, 200, await revoked.text());
    await guest.goBack({ waitUntil: 'domcontentloaded' });
    await guest.waitForFunction(() => {
      const list = document.querySelector('#projects');
      return list?.textContent.includes('GUEST_PRIVATE_MARKER') &&
        !list.textContent.includes('SHARED_PROJECT_MARKER');
    },
      null, { timeout: 5000 });
    assert.match(await guest.locator('#projects').innerText(), /GUEST_PRIVATE_MARKER/);
    assert.doesNotMatch(await guest.locator('#detail-body').innerText(), /SHARED_TASK_MARKER/);
    const forbidden = await fetch(ready.human_endpoint + '/api/workbench/human/projects/' +
      ready.shared + '/task-counts', { headers: { Authorization: 'Bearer ' + guestBearer } });
    assert.notEqual(forbidden.status, 200);

    const recoveredContext = await browser.newContext({ viewport: { width: 907, height: 700 } });
    const recovered = await recoveredContext.newPage();
    recovered.on('pageerror', error => pageErrors.push(error.message));
    await recovered.goto(ready.human_endpoint + '/human');
    await recovered.locator('#recovery-human').fill(ready.guest_id);
    await recovered.locator('#recovery-code').fill(recoveryCode);
    await recovered.locator('#recover-form button[type=submit]').click();
    await recovered.locator('#recovery-receipt').waitFor({ state: 'visible' });
    const rotatedCode = await recovered.locator('#receipt-code').innerText();
    assert.match(rotatedCode, /^amr1_/);
    assert.notEqual(rotatedCode, recoveryCode);
    await recovered.locator('#receipt-close').click();
    await recovered.locator('#workspace').waitFor({ state: 'visible' });
    assert.match(await recovered.locator('#projects').innerText(), /GUEST_PRIVATE_MARKER/);
    assert.doesNotMatch(await recovered.locator('#projects').innerText(), /SHARED_PROJECT_MARKER/);
    await guest.waitForFunction(() => !sessionStorage.getItem('agent-mailbox.human.token'),
      null, { timeout: 5000 });
    assert.equal((await fetch(ready.human_endpoint + '/api/workbench/human/self', {
      headers: { Authorization: 'Bearer ' + guestBearer },
    })).status, 401);
    await recovered.goto(ready.human_endpoint + '/human?history-test=1');
    await recovered.locator('#workspace').waitFor({ state: 'visible' });
    await recovered.locator('#logout').click();
    await recovered.locator('#entry').waitFor({ state: 'visible' });
    await recovered.goBack({ waitUntil: 'domcontentloaded' });
    await recovered.locator('#entry').waitFor({ state: 'visible' });
    assert.equal(await recovered.locator('#workspace').isVisible(), false);
    assert.doesNotMatch(await recovered.locator('body').innerText(), /GUEST_PRIVATE_MARKER|SHARED_PROJECT_MARKER/);
    assert.equal(await recovered.evaluate(() => sessionStorage.getItem('agent-mailbox.human.token')), null);
    await recovered.locator('#recovery-human').fill(ready.guest_id);
    await recovered.locator('#recovery-code').fill(rotatedCode);
    await recovered.locator('#recover-form button[type=submit]').click();
    await recovered.locator('#recovery-receipt').waitFor({ state: 'visible' });
    await recovered.locator('#receipt-close').click();
    await recovered.locator('#workspace').waitFor({ state: 'visible' });
    assert.match(await recovered.locator('#projects').innerText(), /GUEST_PRIVATE_MARKER/);
    const stopped = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('forward stop timed out')), 5000);
      lines.once('line', line => {
        clearTimeout(timeout);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
    });
    child.stdin.write('stop-forward\n');
    assert.deepEqual(await stopped, { forward_stopped: true });
    await recovered.waitForFunction(() =>
      document.querySelector('#workspace').hidden &&
      !document.body.textContent.includes('GUEST_PRIVATE_MARKER'), null, { timeout: 5000 });
    assert.deepEqual(pageErrors, []);
    console.log('REAL_HTTP_TWO_HUMAN_SCOPE_REVOCATION_AND_RECOVERY_OK');
  } finally {
    if (browser) await browser.close();
    child.stdin.end('quit\n');
    await new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      const timeout = setTimeout(() => { child.kill('SIGTERM'); resolve(); }, 5000);
      child.once('exit', () => { clearTimeout(timeout); resolve(); });
    });
    if (child.exitCode !== 0) throw new Error('fixture exit ' + child.exitCode + ': ' + errors());
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
