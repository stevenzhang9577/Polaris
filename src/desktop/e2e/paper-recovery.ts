/** Synthetic browser regression: no model calls, user data or live backend writes. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { launchTestBrowser } from './test-browser';

async function main() {
  const frontend = join(__dirname, '..', '..', 'frontend');
  const socket = createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const address = socket.address();
  assert(address && typeof address !== 'string');
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  const origin = `http://127.0.0.1:${address.port}`;
  const server = spawn(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(address.port), '--strictPort'], { cwd: frontend, stdio: 'pipe' });
  let output = '';
  server.stdout.on('data', (chunk) => { output += String(chunk); });
  server.stderr.on('data', (chunk) => { output += String(chunk); });
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-paper-recovery-'));
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try { ready = (await fetch(`${origin}/e2e/local-integrations.html`)).ok; } catch { /* starting */ }
      if (ready) break;
      if (server.exitCode !== null) throw new Error(output);
      await delay(100);
    }
    assert(ready, output);
    browser = await launchTestBrowser();
    const page = await browser.newPage({ viewport: { width: 1100, height: 940 } });
    const errors: string[] = [];
    page.on('pageerror', (error) => { errors.push(error.message); console.error('Page error:', error.message); });
    await page.addInitScript(() => {
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: { invoke: async (method: string) => method === 'kernel.localBackend' ? { baseUrl: location.origin } : { contract: 5, hostVersion: 'test', platform: 'darwin', capabilities: {} }, subscribe: () => 1, unsubscribe: () => undefined },
      });
    });
    const paperId = '00000000-0000-0000-0000-000000000001';
    const paper = { id: paperId, title: 'Concurrency recovery paper', authors: [], status: 'included', year: 2026,
      relevance_score: null, has_wiki: false, my_tags: [], reading_status: 'unread',
      abstract: 'A synthetic paper used to verify recovery.', concepts: [], tags: [], };
    let listFailures = 3;
    let detailFailures = 3;
    let unavailable = false;
    let detailStatus = 503;
    let listRequests = 0;
    let mutations = 0;
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== 'GET') mutations += 1;
      const reply = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname.endsWith('/libraries/library-summary/papers')) {
        listRequests += 1;
        if (unavailable || listFailures-- > 0) return reply({ detail: 'Synthetic database busy' }, 503);
        return reply({ total: 1, page: 1, size: 20, items: [paper] });
      }
      if (url.pathname.endsWith(`/libraries/library-summary/papers/${paperId}`)) {
        if (unavailable || detailFailures-- > 0) return reply({ detail: 'Synthetic detail unavailable' }, detailStatus);
        return reply(paper);
      }
      if (url.pathname.endsWith('/summary')) return reply({ detail: 'SUMMARY_NOT_FOUND' }, 404);
      if (url.pathname.endsWith('/zotero-local-binding')) return reply({ detail: 'NOT_FOUND' }, 404);
      if (url.pathname.endsWith('/health')) return reply({ status: 'ok' });
      if (/\/(my-tags|libraries|summaries|summary-batches|assets|notes|extractions)$/.test(url.pathname)) return reply([]);
      return reply({ detail: 'NOT_FOUND' }, 404);
    });
    await page.goto(`${origin}/e2e/local-integrations.html?view=paper-recovery`);
    await page.getByText('无法加载论文列表', { exact: true }).waitFor();
    await page.getByRole('button', { name: '重试', exact: true }).waitFor();
    await page.screenshot({ path: join(artifacts, 'paper-list-retry.png'), fullPage: true });
    // No refresh click: recover after all immediate retries were exhausted.
    await page.getByText(paper.title, { exact: true }).waitFor({ timeout: 20000 });
    assert(listRequests >= 4);
    await page.getByText(paper.title, { exact: true }).click();
    await page.getByText('无法加载论文详情', { exact: true }).waitFor();
    await page.getByRole('button', { name: '重试', exact: true }).click();
    await page.getByText('摘要', { exact: true }).click();
    await page.getByText(paper.abstract, { exact: true }).waitFor();
    unavailable = true;
    await page.getByRole('button', { name: 'Refresh fixture', exact: true }).click();
    await page.getByText('刷新暂未成功，正在显示上次加载的内容。', { exact: true }).first().waitFor();
    assert(await page.getByText(paper.title, { exact: true }).count() >= 2);
    assert(await page.getByText(paper.abstract, { exact: true }).isVisible());
    assert.equal(await page.getByText('无法加载论文列表', { exact: true }).count(), 0);
    await page.screenshot({ path: join(artifacts, 'paper-retained-during-retry.png'), fullPage: true });
    unavailable = false;
    await page.getByText('刷新暂未成功，正在显示上次加载的内容。', { exact: true }).first().waitFor({ state: 'hidden', timeout: 20000 });
    detailStatus = 404;
    detailFailures = 100;
    await page.getByRole('button', { name: 'Refresh fixture', exact: true }).click();
    await page.getByText('该资源不存在，或你没有访问权限。', { exact: true }).waitFor();
    assert.equal(await page.getByText(paper.abstract, { exact: true }).count(), 0);
    assert.equal(mutations, 0, 'Read recovery must not create tasks or other writes');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, artifacts, listRequests, mutations }));
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
