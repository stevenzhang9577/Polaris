/** Synthetic browser regression: no model calls, user data or live backend writes. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright-core';

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
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-summary-ui-'));
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
    browser = await chromium.launch({ executablePath: process.env.POLARIS_TEST_BROWSER ?? (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/chromium'), headless: true });
    const page = await browser.newPage({ viewport: { width: 1100, height: 940 } });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: { invoke: async (method: string) => method === 'kernel.localBackend' ? { baseUrl: location.origin } : { contract: 5, hostVersion: 'test', platform: 'darwin', capabilities: {} }, subscribe: () => 1, unsubscribe: () => undefined },
      });
    });
    let concurrency = 3;
    let creates = 0;
    let firstRequestId = '';
    const actions: string[] = [];
    const listPages: number[] = [];
    const detailPages: number[] = [];
    let batch: Record<string, any> | null = null;
    const paperId = (index: number) => `00000000-0000-0000-0000-${String(index).padStart(12, '0')}`;
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      const reply = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname.endsWith('/summary-settings')) {
        if (method === 'PUT') { concurrency = route.request().postDataJSON().concurrency; assert.equal(concurrency, 10); }
        return reply({ concurrency });
      }
      if (url.pathname.endsWith('/summary-batches')) {
        if (method === 'POST') {
          const body = route.request().postDataJSON();
          assert.deepEqual(body.filters, { status: 'library', q: 'robustness', sort: 'relevance' });
          assert.equal(body.paper_ids, undefined);
          assert.deepEqual(body.excluded_ids, [paperId(1)]);
          assert.equal(body.skip_existing, true);
          assert(body.request_id);
          if (++creates === 1) { firstRequestId = body.request_id; return reply({ detail: 'Temporary network failure' }, 503); }
          assert.equal(body.request_id, firstRequestId, 'Retry must keep its request ID');
          batch = { id: 'batch-test', library_id: 'library-summary', status: 'running', total: 6999, pending: 6886, running: 3, completed: 100, skipped: 8, failed: 2, concurrency, created_at: '2026-09-21T00:00:00Z', updated_at: '2026-09-21T00:00:00Z' };
          return reply(batch, 202);
        }
        return reply(batch ? [batch] : []);
      }
      if (url.pathname.includes('/summary-batches/batch-test')) {
        assert(batch);
        if (method === 'POST') {
          const action = url.pathname.split('/').at(-1)!;
          actions.push(action);
          if (action === 'cancel') { batch.status = 'cancelled'; batch.cancelled = batch.pending + batch.running; batch.pending = 0; batch.running = 0; }
          if (action === 'pause') batch.status = 'paused';
          if (action === 'resume') batch.status = 'running';
          if (action === 'retry') { batch.pending += batch.failed; batch.failed = 0; batch.status = 'running'; }
          return reply(batch);
        }
        const pageNumber = Number(url.searchParams.get('page'));
        detailPages.push(pageNumber);
        return reply({ batch, page: pageNumber, size: 20, total: batch.total, items: Array.from({ length: 20 }, (_, index) => ({ paper_id: paperId((pageNumber - 1) * 20 + index + 2), title: `Robustness paper ${(pageNumber - 1) * 20 + index + 2}`, status: batch!.status === 'cancelled' ? 'cancelled' : index === 0 && batch!.failed > 0 ? 'failed' : index === 1 ? 'running' : 'pending', stage: index === 1 ? 'compile' : null, error: index === 0 && batch!.failed > 0 ? 'Synthetic provider timeout' : null })) });
      }
      if (url.pathname.endsWith('/libraries/library-summary/papers')) {
        const pageNumber = Number(url.searchParams.get('page'));
        listPages.push(pageNumber);
        return reply({ total: 7000, page: pageNumber, size: 20, items: Array.from({ length: 20 }, (_, index) => ({ id: paperId((pageNumber - 1) * 20 + index + 1), title: `Robustness paper ${(pageNumber - 1) * 20 + index + 1}`, authors: [], status: 'included', year: 2026, relevance_score: null, has_wiki: false, my_tags: [], reading_status: 'unread' })) });
      }
      if (url.pathname.endsWith('/zotero-local-binding')) return reply({ detail: 'NOT_FOUND' }, 404);
      if (url.pathname.endsWith('/my-tags') || url.pathname.endsWith('/libraries')) return reply([]);
      if (url.pathname.endsWith('/health')) return reply({ status: 'ok' });
      return reply([]);
    });

    await page.goto(`${origin}/e2e/local-integrations.html?view=summary-batches`);
    await page.getByRole('button', { name: '全选筛选结果 (7000)', exact: true }).waitFor();
    await page.getByPlaceholder('搜索标题 / 关键词…').fill('robustness');
    await page.waitForTimeout(700);
    await page.getByRole('button', { name: '多选', exact: true }).click();
    await page.getByRole('checkbox', { name: '选择论文：Robustness paper 1', exact: true }).check();
    await page.getByRole('button', { name: '全选筛选结果 (7000)', exact: true }).click();
    await page.getByRole('checkbox', { name: '选择论文：Robustness paper 1', exact: true }).uncheck();
    await page.getByText('已选全部筛选结果中的 6999 篇，包含未加载页面。', { exact: true }).waitFor();
    assert(listPages.every((value) => value === 1), 'Select all must not fetch every page');
    await page.getByRole('button', { name: '加载更多', exact: true }).click();
    await page.getByRole('checkbox', { name: '选择论文：Robustness paper 21', exact: true }).waitFor();
    assert(await page.getByRole('checkbox', { name: '选择论文：Robustness paper 21', exact: true }).isChecked());
    await page.getByRole('button', { name: '生成总结', exact: true }).first().click();
    const createDialog = page.getByRole('dialog', { name: '批量生成论文总结', exact: true });
    await createDialog.getByText('已选择 6999 篇论文', { exact: true }).waitFor();
    assert(await createDialog.getByRole('checkbox').isChecked());
    await createDialog.getByRole('button', { name: '开始生成', exact: true }).click();
    await page.getByText(/创建失败，可重试/).waitFor();
    await createDialog.getByRole('button', { name: '开始生成', exact: true }).click();
    const progress = page.getByRole('dialog', { name: '论文总结任务', exact: true });
    await progress.getByText('Synthetic provider timeout', { exact: true }).waitFor();
    await progress.getByRole('button', { name: '暂停任务', exact: true }).click();
    await progress.getByText('暂停中，等待当前论文完成', { exact: true }).waitFor();
    await progress.getByRole('button', { name: '继续任务', exact: true }).click();
    await progress.getByRole('button', { name: '暂停任务', exact: true }).waitFor();
    await progress.getByRole('button', { name: '重试失败 2 篇', exact: true }).click();
    await progress.getByText('失败 0', { exact: true }).waitFor();
    await progress.getByRole('button', { name: '下一页', exact: true }).click();
    await progress.getByText('Robustness paper 22', { exact: true }).waitFor();
    assert(detailPages.includes(2));
    await page.screenshot({ path: join(artifacts, 'batch-cancel-button.png'), fullPage: true });
    await progress.getByRole('button', { name: '取消任务', exact: true }).click();
    await progress.getByText('已取消', { exact: true }).first().waitFor();
    assert.equal(await progress.getByRole('button', { name: '继续任务', exact: true }).count(), 0);
    assert.deepEqual(actions, ['pause', 'resume', 'retry', 'cancel']);
    await page.screenshot({ path: join(artifacts, 'batch-progress.png'), fullPage: true });
    await progress.getByRole('button', { name: '完成', exact: true }).click();
    for (const width of [1100, 820, 430]) {
      await page.setViewportSize({ width, height: 940 });
      await page.screenshot({ path: join(artifacts, `batch-selection-${width}.png`), fullPage: true });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Horizontal overflow at ${width}`);
    }
    await page.getByPlaceholder('搜索标题 / 关键词…').fill('another');
    await page.getByRole('button', { name: '多选', exact: true }).waitFor();
    assert(await page.getByRole('button', { name: '生成总结', exact: true }).first().isDisabled(), 'Changing filters must reset selection');
    await page.goto(`${origin}/e2e/local-integrations.html?view=summary-settings`);
    await page.getByLabel('同时生成的论文数', { exact: true }).waitFor();
    await page.getByLabel('同时生成的论文数', { exact: true }).fill('21');
    assert(await page.getByRole('button', { name: '保存', exact: true }).isDisabled());
    await page.getByLabel('同时生成的论文数', { exact: true }).fill('10');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByText('论文总结并发数已保存', { exact: true }).waitFor();
    await page.reload();
    await page.waitForFunction(() => (document.getElementById('summary-concurrency') as HTMLInputElement)?.value === '10');
    await page.waitForTimeout(500); // Let the page fade-in finish before visual verification.
    await page.screenshot({ path: join(artifacts, 'summary-settings-430.png'), fullPage: true });
    assert.equal(concurrency, 10);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, artifacts, selected: 6999, creates, actions, concurrency }));
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
