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
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-summary-provenance-'));
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
    page.on('pageerror', (error) => { errors.push(error.message); console.error('Page error:', error.message); });
    await page.addInitScript(() => {
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: { invoke: async (method: string) => method === 'kernel.localBackend' ? { baseUrl: location.origin } : { contract: 5, hostVersion: 'test', platform: 'darwin', capabilities: {} }, subscribe: () => 1, unsubscribe: () => undefined },
      });
    });
    const paperId = 'paper-provenance';
    const paper = { id: paperId, title: 'Summary provenance regression', authors: [], concepts: [], my_tags: [],
      status: 'included', has_wiki: false, reading_status: 'unread', relevance_score: null };
    const readyRevision = { id: 'ready', paper_id: paperId, model: 'kimi-k3', requested_model: 'kimi-k3[1M]',
      content: '## TL;DR\nA usable current summary.', source_level: 'fulltext', status: 'ready', stage: 'complete',
      is_current: true, created_at: '2026-09-21T00:03:00', updated_at: '2026-09-21T00:03:00' };
    const old = { ...readyRevision, id: 'old-failure', model: null, requested_model: null, content: null,
      status: 'failed', stage: null, is_current: false, error_code: 'SUMMARY_GENERATION_FAILED',
      error_detail: 'SUMMARY_GENERATION_FAILED', created_at: '2026-09-20T22:43:00' };
    let recent = false;
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      const reply = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (path.endsWith('/libraries/library-summary/papers')) return reply({ page: 1, size: 20, total: 1, items: [paper] });
      if (path.endsWith(`/libraries/library-summary/papers/${paperId}`)) return reply(paper);
      if (path.endsWith('/summary')) return reply({ paper_id: paperId, current_revision: readyRevision, stale: false });
      if (path.endsWith('/summaries')) return reply([...(recent ? [{ ...old, id: 'protocol-failure', requested_model: 'kimi-k3[1M]', model: 'glm-5.3-flash', error_code: 'LLM_PROVIDER_PROTOCOL_MISMATCH', error_detail: 'LLM_PROVIDER_PROTOCOL_MISMATCH', created_at: '2026-09-21T04:59:00' }] : []), readyRevision, old]);
      if (path.endsWith('/content-version')) return reply({ id: 'content', version_no: 1, parser: 'pymupdf', status: 'ready_fallback', page_count: 17, chunk_count: 17, document_vector_state: 'failed', chunk_vector_state: 'failed', error_code: 'VECTOR_BUILD_FAILED', error_detail: 'NotImplementedError: no embedding model configured' });
      if (path.endsWith('/assets')) return reply({ items: [] });
      if (/\/(summary-batches|my-tags|notes|extractions)$/.test(path)) return reply([]);
      if (path.endsWith('/health')) return reply({ status: 'ok' });
      return reply({ detail: 'NOT_FOUND' }, 404);
    });
    await page.goto(`${origin}/e2e/local-integrations.html?view=paper-recovery`);
    await page.getByTitle(paper.title, { exact: true }).click();
    await page.getByRole('button', { name: '历史 2', exact: true }).click();
    await page.getByText('此历史尝试生成失败，未保存具体原因', { exact: true }).waitFor();
    assert.equal(await page.getByText('最近一次生成失败', { exact: false }).count(), 0);
    await page.getByText(/尚未配置向量嵌入模型；语义检索暂不可用/).waitFor();
    assert.equal(await page.getByText(/VECTOR_BUILD_FAILED/).count(), 0);
    await page.waitForTimeout(500); // Let the paper pane's fade-in finish.
    await page.screenshot({ path: join(artifacts, 'ready-summary-missing-embedding.png'), fullPage: true });
    recent = true;
    await page.getByRole('button', { name: 'Refresh fixture', exact: true }).click();
    await page.getByText('请求 kimi-k3[1M] · 返回 glm-5.3-flash', { exact: true }).waitFor();
    await page.getByText(/最近一次生成失败：模型网关返回格式/).waitFor();
    assert.equal(await page.getByText('未知模型', { exact: true }).count(), 0);
    await page.screenshot({ path: join(artifacts, 'summary-protocol-mismatch.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, artifacts }));
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
