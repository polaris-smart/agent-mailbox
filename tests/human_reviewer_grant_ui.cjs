require('./browser_sandbox_guard.cjs');
// Real Human assets, synthetic API: reviewer grants must be explicit and visible.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const assets = path.resolve(process.env.MAILBOX_UI_ASSETS || path.join(__dirname, '../src/agent_mailbox/workbench_assets'));

async function openHuman(browser, {width, role, canReview, onWrite}) {
  const context = await browser.newContext({viewport: {width, height: 700}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    sessionStorage.setItem('agent-mailbox.human.token', 'synthetic-human-token');
    const original = window.fetch.bind(window);
    window.fetch = (url, opts = {}) => url === '/api/workbench/human/changes'
      ? Promise.resolve(new Response(new ReadableStream({start(c) {
          c.enqueue(new TextEncoder().encode('{"ready":true}\n'));
          window.__emitHumanChange = event => c.enqueue(new TextEncoder().encode(JSON.stringify(event) + '\n'));
          opts.signal?.addEventListener('abort', () => c.close(), {once: true});
        }}), {headers: {'Content-Type': 'application/x-ndjson'}}))
      : original(url, opts);
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.host !== 'mailbox.test') return route.abort();
    const base = '/api/workbench/human/projects/project_one';
    if (url.pathname === '/api/workbench/human/self') return route.fulfill({json: {human_id: `human_${role}`, display_name: role}});
    if (url.pathname === '/api/workbench/human/spaces') return route.fulfill({json: {spaces: [{id: 'space_shared', name: 'Family', kind: 'shared', role: role === 'owner' ? 'owner' : 'member'}]}});
    if (url.pathname === '/api/workbench/human/projects') return route.fulfill({json: {projects: [{id: 'project_one', name: 'Family project', space_id: 'space_shared', kind: 'shared', role, can_review: role === 'owner' ? 1 : Number(canReview())}]}});
    if (url.pathname === base + '/tasks') return route.fulfill({json: {tasks: [{id: 'task_one', title: 'Synthetic task', status: 'review'}]}});
    if (url.pathname === base + '/messages') return route.fulfill({json: {messages: []}});
    if (url.pathname === base + '/resources/manifest') return route.fulfill({json: {resources: []}});
    if (url.pathname === base + '/tasks/task_one/review' && route.request().method() === 'GET') return route.fulfill({json: {
      task_id: 'task_one', title: 'Synthetic task', status: 'review', summary: 'SYNTHETIC_PRIVATE_DELIVERY',
      verification: {status: 'employee_report'}, files: [], diff: '', capture_error: null,
    }});
    if (url.pathname === '/api/workbench/human/spaces/space_shared/members') return route.fulfill({json: {members: [
      {id: 'human_owner', display_name: 'Owner', role: 'owner'},
      {id: 'human_viewer', display_name: 'Viewer', role: 'member'},
    ]}});
    if (url.pathname === base + '/grants') return route.fulfill({json: {grants: [
      {human_id: 'human_owner', display_name: 'Owner', role: 'owner', can_review: 1},
      {human_id: 'human_viewer', display_name: 'Viewer', role: 'viewer', can_review: Number(canReview())},
    ]}});
    if (url.pathname.startsWith(base + '/reviewers')) {
      onWrite(route.request());
      return route.fulfill({json: {ok: true}});
    }
    if (url.pathname.startsWith('/api/')) return route.fulfill({status: 404, json: {error: {message: 'Unexpected API'}}});
    const name = url.pathname === '/human' ? 'human.html' : path.basename(url.pathname);
    return route.fulfill({body: fs.readFileSync(path.join(assets, name)), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
  });
  await page.goto('http://mailbox.test/human');
  await page.getByRole('button', {name: 'Family project'}).click();
  return {page, context, errors};
}

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    for (const width of [375, 900]) {
      let granted = false;
      const writes = [];
      const owner = await openHuman(browser, {width, role: 'owner', canReview: () => granted,
        onWrite(request) {
          writes.push({method: request.method(), path: new URL(request.url()).pathname});
          granted = request.method() === 'POST';
        }});
      try {
        await owner.page.getByRole('button', {name: '授予验收 · Viewer'}).click();
        await owner.page.getByRole('button', {name: '取消', exact: true}).click();
        assert.deepEqual(writes, []);
        await owner.page.getByRole('button', {name: '授予验收 · Viewer'}).click();
        if (process.env.UI_SCREENSHOT_DIR) {
          fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, {recursive: true});
          await owner.page.screenshot({path: path.join(process.env.UI_SCREENSHOT_DIR, `reviewer-grant-${width}.png`)});
        }
        await owner.page.getByRole('button', {name: '确认调整'}).click();
        await owner.page.getByRole('button', {name: '撤销验收 · Viewer'}).waitFor();
        assert.deepEqual(writes, [{method: 'POST', path: '/api/workbench/human/projects/project_one/reviewers'}]);
        await owner.page.getByRole('button', {name: '撤销验收 · Viewer'}).click();
        await owner.page.getByRole('button', {name: '确认调整'}).click();
        await owner.page.getByRole('button', {name: '授予验收 · Viewer'}).waitFor();
        assert.equal(writes.at(-1).method, 'DELETE');
        assert.equal(writes.at(-1).path, '/api/workbench/human/projects/project_one/reviewers/human_viewer');
        assert.equal(await owner.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        assert.deepEqual(owner.errors, []);
      } finally {await owner.context.close();}

      let reviewerGranted = true;
      const reviewer = await openHuman(browser, {width, role: 'viewer', canReview: () => reviewerGranted,
        onWrite() {throw new Error('reviewer attempted owner write');}});
      try {
        await reviewer.page.getByRole('button', {name: '查看交付并验收'}).waitFor();
        assert.equal(await reviewer.page.getByRole('button', {name: '查看交付并验收'}).count(), 1);
        assert.equal(await reviewer.page.locator('.share-tools').count(), 0);
        await reviewer.page.getByRole('button', {name: '查看交付并验收'}).click();
        await reviewer.page.getByText('SYNTHETIC_PRIVATE_DELIVERY').waitFor();
        reviewerGranted = false;
        await reviewer.page.evaluate(() => window.__emitHumanChange({access_changed: true}));
        await reviewer.page.locator('#review-dialog').waitFor({state: 'hidden'});
        await reviewer.page.waitForFunction(() => document.getElementById('review-summary').textContent === '');
        assert.equal(await reviewer.page.locator('#review-summary').textContent(), '');
        assert.equal(await reviewer.page.getByRole('button', {name: '查看交付并验收'}).count(), 0);
        assert.deepEqual(reviewer.errors, []);
      } finally {await reviewer.context.close();}
      const plainViewer = await openHuman(browser, {width, role: 'viewer', canReview: () => false,
        onWrite() {throw new Error('viewer attempted owner write');}});
      try {
        await plainViewer.page.getByText('Synthetic task').waitFor();
        assert.equal(await plainViewer.page.getByRole('button', {name: '查看交付并验收'}).count(), 0);
        assert.deepEqual(plainViewer.errors, []);
      } finally {await plainViewer.context.close();}
      console.log(`${width}px reviewer grant and visibility PASS`);
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
