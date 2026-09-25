/** Synthetic regression for capability-aware provider probes and equal-height library cards. */
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
  const server = spawn(process.execPath, [
    join(frontend, 'node_modules/vite/bin/vite.js'),
    '--host', '127.0.0.1', '--port', String(address.port), '--strictPort',
  ], { cwd: frontend, stdio: 'pipe' });
  let output = '';
  server.stdout.on('data', (chunk) => { output += String(chunk); });
  server.stderr.on('data', (chunk) => { output += String(chunk); });
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-provider-library-ui-'));
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      try { ready = (await fetch(`${origin}/e2e/llm-usage.html`)).ok; } catch { /* starting */ }
      if (ready) break;
      if (server.exitCode !== null) throw new Error(output);
      await delay(100);
    }
    assert(ready, output);
    browser = await launchTestBrowser();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors: string[] = [];
    const probes: unknown[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: {
          invoke: async (method: string) => method === 'kernel.localBackend'
            ? { baseUrl: location.origin }
            : { contract: 5, hostVersion: 'test', platform: 'darwin', capabilities: {} },
          subscribe: () => 1,
          unsubscribe: () => undefined,
        },
      });
    });

    const provider = {
      id: 'openrouter', name: 'openrouter', kind: 'openai_compat', transport: 'chat_completions',
      auth_scheme: 'bearer', enabled: true, api_key_masked: '***',
      base_url: 'https://openrouter.ai/api/v1', user_agent: null,
      models: ['qwen/qwen3-embedding-8b'], model_pricing: {},
      import_source: null, import_source_key: null, import_fingerprint: null, imported_at: null,
    };
    const now = '2026-09-21T06:39:00Z';
    const library = (id: string, name: string, papers: number, concepts: number) => ({
      id, name, library_kind: 'standard', interdisciplinary_domains: null, discipline: null,
      statement: null, project_id: null, is_mine: false, can_manage: true, is_public: false,
      owner_name: 'Local', is_owner: true, submitted_by: 'user', paper_count: papers,
      concept_count: concepts, last_compiled_at: papers ? now : null,
      last_synced_at: papers ? now : null, created_at: now, updated_at: now,
    });
    const libraries = [
      library('a', 'Adversarial Learning', 2989, 133),
      library('b', 'Diffusion & Generative Model Attacks', 163, 0),
      library('c', 'LLM Safety & Attacks', 393, 2),
      library('d', 'LLM Training & Agents', 0, 0),
    ];
    // Keep a bound and an unbound card in the first row; both must reserve the
    // same source-status slot or the cards below will drift vertically.
    const bindings = [libraries[0]!, libraries[2]!].map((item) => ({
      id: `binding-${item.id}`, library_id: item.id, zotero_library_id: '1',
      zotero_library_type: 'user', zotero_instance_id: null, collection_key: item.id,
      collection_name: item.name,
      include_descendants: true, last_library_version: 1, status: 'idle',
      last_synced_at: now, next_sync_at: null, last_error: null, created_at: now, updated_at: now,
    }));

    await page.route('**/api/**', async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const reply = (value: unknown, status = 200) => route.fulfill({
        status, contentType: 'application/json', body: JSON.stringify(value),
      });
      if (path.endsWith('/admin/llm/test-model')) {
        const body = request.postDataJSON();
        probes.push(body);
        return reply({ ok: body.capability === 'embedding', latency_ms: 1339,
          error: body.capability === 'embedding' ? null : 'wrong capability' });
      }
      if (path.endsWith('/admin/llm/providers')) return reply([provider]);
      if (path.endsWith('/users/me')) return reply({ id: 'user', email: 'local@example.test', name: 'Local' });
      if (path.endsWith('/zotero-local/bindings')) return reply(bindings);
      if (path.endsWith('/libraries')) return reply(libraries);
      if (path.endsWith('/health')) return reply({ status: 'ok', version: 'test' });
      return reply([]);
    });

    await page.goto(`${origin}/e2e/llm-usage.html?view=providers`);
    await page.getByText('qwen/qwen3-embedding-8b', { exact: true }).waitFor();
    await page.getByRole('button', { name: '对话', exact: true }).click();
    await page.getByRole('option', { name: '向量嵌入', exact: true }).click();
    await page.getByText('未测试', { exact: true }).click();
    await page.getByText('正常 · 1,339ms', { exact: true }).waitFor();
    assert.deepEqual(probes, [{ provider_id: 'openrouter', model: 'qwen/qwen3-embedding-8b', capability: 'embedding' }]);

    const providerRow = page.getByRole('row').filter({ hasText: 'openrouter' });
    await providerRow.getByTitle('编辑').click();
    const dialog = page.getByRole('dialog', { name: '编辑 Provider' });
    const baseUrl = dialog.locator('.field').filter({ hasText: 'Base URL' }).locator('input');
    const save = dialog.getByRole('button', { name: '保存', exact: true });
    await baseUrl.fill('https://openrouter.ai/api/v1/embeddings');
    assert(await save.isDisabled(), 'an embeddings endpoint must not be stored as the API root');
    await dialog.getByText('这里需要根地址，请去掉末尾的具体接口路径。').waitFor();
    await dialog.getByRole('button', { name: 'OpenAI Chat Completions', exact: true }).click();
    await page.getByRole('option', { name: 'OpenAI Responses', exact: true }).click();
    await baseUrl.fill('https://openrouter.ai/api/v1/responses');
    assert(await save.isEnabled(), 'a complete /responses URL must remain valid for Responses transport');
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await page.screenshot({ path: join(artifacts, 'embedding-provider-test.png'), fullPage: true });

    await page.goto(`${origin}/e2e/local-integrations.html?view=libraries`);
    await page.getByText('Diffusion & Generative Model Attacks', { exact: true }).first().waitFor();
    await page.evaluate(async () => { await document.fonts.ready; });
    const layout = await page.locator('.card.hoverable').evaluateAll((cards) => cards.slice(0, 3).map((card) => {
      const box = card.getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, height: box.height };
    }));
    assert.equal(layout.length, 3);
    assert(Math.max(...layout.map((item) => item.top)) - Math.min(...layout.map((item) => item.top)) < 1);
    assert(Math.max(...layout.map((item) => item.bottom)) - Math.min(...layout.map((item) => item.bottom)) < 1);
    const longTitleCard = page.locator('.card.hoverable').filter({
      has: page.locator('span[title="Diffusion & Generative Model Attacks"]'),
    });
    const title = await longTitleCard.locator('span[title="Diffusion & Generative Model Attacks"]').boundingBox();
    const badge = await longTitleCard.getByText('个人', { exact: true }).boundingBox();
    assert(title && badge);
    assert(Math.abs(title.y - badge.y) < 2, 'type badge must stay aligned with a two-line title');

    const pathBeforeKeyboardAction = new URL(page.url()).pathname;
    await page.getByTitle('批量选择', { exact: true }).click();
    await page.getByTitle('选择', { exact: true }).first().press('Space');
    await page.getByText('已选 1 个', { exact: true }).first().waitFor();
    assert.equal(new URL(page.url()).pathname, pathBeforeKeyboardAction,
      'keyboard activation of a nested checkbox must not open the card');
    await page.screenshot({ path: join(artifacts, 'library-card-grid.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: join(artifacts, 'library-card-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, artifacts }));
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
