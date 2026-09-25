/** Shared usage UI with synthetic API data, no user profile or credentials. */
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
  const port = address.port;
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'),
    '--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: frontend, stdio: 'pipe' });
  let output = '';
  server.stdout?.on('data', (chunk) => { output += String(chunk); });
  server.stderr?.on('data', (chunk) => { output += String(chunk); });
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-usage-ui-'));
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try { ready = (await fetch(`${origin}/e2e/llm-usage.html`)).ok; } catch { /* starting */ }
      if (ready) break;
      if (server.exitCode !== null) throw new Error(output);
      await delay(100);
    }
    assert(ready, output);
    browser = await launchTestBrowser();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
    const errors: string[] = [];
    const paths: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const provider = {
      id: 'provider-fixture', name: 'Research API', kind: 'openai_compat', transport: 'chat_completions',
      auth_scheme: 'bearer', enabled: true, api_key_masked: '', base_url: null, user_agent: null,
      models: ['research-model', 'fast-model'], model_pricing: {} as Record<string, unknown>,
    };
    const rows = [
      { date: '2026-09-21', stage: 'reading', provider_name: 'Research API', model: 'research-model',
        prompt_tokens: 1200000, completion_tokens: 80000, cache_read_tokens: 900000, cache_creation_tokens: 0,
        calls: 24, cache_reported_calls: 24, estimated_calls: 0, priced_calls: 24, cost_usd: '1.42' },
      { date: '2026-09-20', stage: 'librarian', provider_name: 'Research API', model: 'fast-model',
        prompt_tokens: 500000, completion_tokens: 40000, cache_read_tokens: 200000, cache_creation_tokens: 50000,
        calls: 12, cache_reported_calls: 10, estimated_calls: 2, priced_calls: 10, cost_usd: '0.245' },
      { date: '2026-09-19', stage: 'default', provider_name: null, model: 'legacy-model',
        prompt_tokens: 100000, completion_tokens: 10000, cache_read_tokens: 0, cache_creation_tokens: 0,
        calls: 2, cache_reported_calls: 0, estimated_calls: 2, priced_calls: 0, cost_usd: null },
    ];
    let routes = [{ stage: 'default', provider_id: provider.id, model: 'research-model' }];
    await page.route('**/api/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      paths.push(url.pathname);
      let data: unknown = {};
      if (url.pathname.endsWith('/llm/routes')) {
        if (request.method() === 'PUT') routes = request.postDataJSON();
        data = routes;
      } else if (url.pathname.endsWith('/usage/calls')) data = { total: 1, items: [{ ...rows[0], id: 'call-1', occurred_at: '2026-09-21T02:39:25Z', calls: 1, cache_reported_calls: 1, priced_calls: 1, prompt_tokens: 1000, completion_tokens: 100, cache_read_tokens: 750, cost_usd: '0.00142', reference_cost_usd: '0.0012' }] };
      else if (url.pathname.endsWith('/cc-switch-pricing')) data = { 'fast-model': { input_per_million: '1', output_per_million: '4', cache_read_per_million: '0.1', cache_creation_per_million: '0' } };
      else if (url.pathname.endsWith('/usage/history') || url.pathname.endsWith('/llm/usage')) data = rows;
      else if (request.method() === 'PATCH' && url.pathname.endsWith(provider.id)) {
        provider.model_pricing = request.postDataJSON().model_pricing;
        data = provider;
      } else if (url.pathname.endsWith('/llm/providers')) data = [provider];
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto(`${origin}/e2e/llm-usage.html`);
    await page.getByText('按模型汇总', { exact: true }).waitFor();
    assert(paths.includes('/api/users/me/usage/history'));
    await page.getByText('逐次调用明细 · 本地时间', { exact: true }).waitFor();
    assert.match(await page.locator('.usage-stats').innerText(), /1,800,000/);
    assert.match(await page.locator('.usage-stats').innerText(), /61\.1%/);
    assert.match(await page.locator('.usage-stats').innerText(), /\$1\.665/);
    await page.screenshot({ path: join(artifacts, 'usage-dashboard.png'), fullPage: true });
    await page.getByRole('button', { name: '全部模型' }).click();
    await page.getByRole('option', { name: 'research-model', exact: true }).click();
    assert.match(await page.locator('.usage-stats').innerText(), /75\.0%/);
    assert.equal(await page.locator('.usage-table').first().locator('tbody tr').count(), 1);
    const changedRange = page.waitForResponse((response) => response.url().includes('days=7'));
    await page.getByRole('button', { name: '7 天', exact: true }).click();
    await changedRange;
    await page.goto(`${origin}/e2e/llm-usage.html?view=platform`);
    await page.getByText('按模型汇总', { exact: true }).waitFor();
    assert(paths.includes('/api/admin/llm/usage'));
    await page.getByLabel('切换默认模型', { exact: true }).selectOption(JSON.stringify([provider.id, 'fast-model']));
    await page.getByRole('button', { name: '设为默认调用模型', exact: true }).click();
    await page.getByText(/默认路由已切换/).waitFor();
    assert.equal(routes[0]?.model, 'fast-model');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: join(artifacts, 'usage-mobile.png'), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.goto(`${origin}/e2e/llm-usage.html?view=pricing`);
    await page.getByRole('button', { name: '从本机 CC Switch 导入匹配单价', exact: true }).click();
    await page.getByText(/已导入匹配单价/).waitFor();
    assert(provider.model_pricing['fast-model']);
    await page.getByLabel('模型 ID', { exact: true }).fill('research-model');
    await page.getByLabel('输入单价', { exact: true }).fill('2');
    await page.getByLabel('输出单价', { exact: true }).fill('8');
    await page.getByLabel('缓存读取单价（可选）', { exact: true }).fill('0.2');
    await page.getByRole('button', { name: '保存模型单价' }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).waitFor();
    assert.deepEqual(provider.model_pricing['research-model'], {
      input_per_million: '2', output_per_million: '8', cache_read_per_million: '0.2', cache_creation_per_million: null,
    });
    await page.screenshot({ path: join(artifacts, 'model-pricing.png'), fullPage: true });
    await page.getByRole('button', { name: '移除单价', exact: true }).first().click();
    await page.waitForFunction(() => document.querySelectorAll('.usage-table tbody tr').length === 1);
    await page.getByRole('button', { name: '移除单价', exact: true }).click();
    await page.getByText('尚未配置单价，用量将记录为费用未知。').waitFor();
    assert.deepEqual(provider.model_pricing, {});
    assert.deepEqual(errors, []);
    console.log(`Usage UI checks passed. Screenshots: ${artifacts}`);
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
