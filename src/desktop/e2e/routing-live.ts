/** Real browser -> FastAPI -> SQLite -> router -> local HTTP provider regression. */
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { createServer as createSocket } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright-core';

async function freePort() {
  const socket = createSocket().listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const address = socket.address();
  assert(address && typeof address !== 'string');
  await new Promise<void>(resolve => socket.close(() => resolve()));
  return address.port;
}

async function main() {
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-routing-live-'));
  const backend = resolve('../backend');
  const frontend = resolve('../frontend');
  const apiPort = await freePort();
  const webPort = await freePort();
  const apiOrigin = `http://127.0.0.1:${apiPort}`;
  const webOrigin = `http://127.0.0.1:${webPort}`;
  const seen: string[] = [];
  let wrongProtocol = false;
  const upstream = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    const request = JSON.parse(Buffer.concat(chunks).toString());
    seen.push(request.model);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(req.url?.endsWith('/messages') && !wrongProtocol ? {
      id: 'msg-fixture', type: 'message', role: 'assistant', model: request.model,
      content: [{ type: 'text', text: 'routing verified' }], stop_reason: 'end_turn',
      usage: { input_tokens: 10, output_tokens: 2, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
    } : {
      id: 'chat-fixture', model: wrongProtocol ? 'glm-5.3-flash' : request.model,
      choices: [{ message: { role: 'assistant', content: 'routing verified' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 10, completion_tokens: 2, prompt_tokens_details: { cached_tokens: 0 } },
    }));
  }).listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const address = upstream.address();
  assert(address && typeof address !== 'string');
  let browser;
  const children: ChildProcess[] = [];
  let logs = '';
  const start = (command: string, args: string[], cwd: string, extra: Record<string, string>) => {
    const child = spawn(command, args, { cwd, env: { ...process.env, ...extra }, stdio: 'pipe' });
    child.stdout?.on('data', chunk => { logs += String(chunk); });
    child.stderr?.on('data', chunk => { logs += String(chunk); });
    children.push(child);
    return child;
  };
  try {
    start(join(backend, '.venv/bin/python'), ['-m', 'tests.routing_ui_server'], backend, {
      POLARIS_DATABASE_URL: `sqlite+aiosqlite:///${artifacts}/test.db`,
      POLARIS_DATA_DIR: join(artifacts, 'data'), POLARIS_PROFILE: 'desktop', POLARIS_ENV: 'dev',
      POLARIS_TEST_API_PORT: String(apiPort), POLARIS_LLM_FAKE_FALLBACK: '0',
      POLARIS_SECRET_KEY: 'isolated-routing-fixture-secret-0123456789abcdef',
      POLARIS_ENCRYPTION_KEY: '',
    });
    start(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(webPort), '--strictPort'], frontend, { VITE_PROXY_TARGET: apiOrigin });
    for (const url of [`${apiOrigin}/api/health`, `${webOrigin}/e2e/llm-usage.html`]) {
      let ready = false;
      for (let i = 0; i < 150; i++) {
        try { ready = (await fetch(url)).ok; } catch { /* startup */ }
        if (ready) break;
        if (children.some(child => child.exitCode !== null)) throw new Error(logs);
        await delay(100);
      }
      assert(ready, logs);
    }
    const session = await (await fetch(`${apiOrigin}/api/auth/local-session`, { method: 'POST' })).json() as { access_token: string };
    const headers = { Authorization: `Bearer ${session.access_token}`, 'Content-Type': 'application/json' };
    async function api(path: string, method = 'GET', body?: unknown): Promise<any> {
      const response = await fetch(`${apiOrigin}/api${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
      assert(response.ok, `${path}: ${response.status}`);
      return response.json();
    }
    const oldProvider = await api('/admin/llm/providers', 'POST', { name: 'Codex · OpenAI', kind: 'openai_compat', auth_scheme: 'none', base_url: `http://127.0.0.1:${address.port}/v1`, models: ['gpt-6-astra'] });
    const newProvider = await api('/admin/llm/providers', 'POST', { name: 'Claude Code', kind: 'anthropic', auth_scheme: 'none', base_url: `http://127.0.0.1:${address.port}`, models: ['kimi-k3[1M]'] });
    await api('/admin/llm/routes', 'PUT', [{ stage: 'default', provider_id: oldProvider.id, model: 'gpt-6-astra' }]);
    assert.equal((await api('/test-routing/call', 'POST')).model, 'gpt-6-astra');
    browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(token => localStorage.setItem('polaris.token', token), session.access_token);
    await page.goto(`${webOrigin}/e2e/llm-usage.html?view=routing`);
    const row = page.locator('tbody tr').filter({ has: page.getByText('default', { exact: true }) });
    await row.getByRole('button', { name: 'Codex · OpenAI', exact: true }).click();
    await page.getByRole('option', { name: 'Claude Code', exact: true }).click();
    await page.getByRole('button', { name: '保存路由表', exact: true }).click();
    await page.getByText(/请选择供应商并填写模型/).waitFor();
    assert.equal((await api('/admin/llm/routes'))[0].model, 'gpt-6-astra');
    await row.locator('input').first().fill('kimi-k3[1M]');
    // Query refetch must not overwrite a user's unsaved edit.
    const refetch = page.waitForResponse(response => response.url().endsWith('/api/admin/llm/routes') && response.request().method() === 'GET');
    await page.evaluate(() => (window as any).refetchRoutingForTest());
    await refetch;
    assert.equal(await row.locator('input').first().inputValue(), 'kimi-k3[1M]');
    await page.getByRole('button', { name: '保存路由表', exact: true }).click();
    await page.getByText('模型路由表已保存，后续调用按新路由执行', { exact: true }).waitFor();
    const routes = await api('/admin/llm/routes');
    assert.equal(routes[0].model, 'kimi-k3[1M]');
    assert.equal(routes[0].provider_id, newProvider.id);
    assert.equal((await api('/test-routing/call', 'POST')).model, 'kimi-k3[1M]');
    assert.deepEqual(seen, ['gpt-6-astra', 'kimi-k3[1M]']);
    await page.reload();
    await row.getByRole('button', { name: 'Claude Code', exact: true }).waitFor();
    assert.equal(await row.locator('input').first().inputValue(), 'kimi-k3[1M]');
    const usage = await api('/users/me/usage/calls');
    assert.equal(usage.items[0].provider_name, 'Claude Code');
    assert.equal(usage.items[0].model, 'kimi-k3[1M]');
    await page.screenshot({ path: join(artifacts, 'saved-route.png'), fullPage: true });
    wrongProtocol = true;
    const failed = await fetch(`${apiOrigin}/api/test-routing/call`, { method: 'POST', headers });
    assert.equal(failed.status, 502);
    assert.deepEqual(await failed.json(), { code: 'LLM_PROVIDER_PROTOCOL_MISMATCH', requested: 'kimi-k3[1M]', returned: 'glm-5.3-flash' });
    const failureUsage = (await api('/users/me/usage/calls')).items[0];
    assert.equal(failureUsage.requested_model, 'kimi-k3[1M]');
    assert.equal(failureUsage.model, 'glm-5.3-flash');
    assert.equal(failureUsage.completion_tokens, 2);
    assert.equal(failureUsage.cost_usd, null);
    const probe = await api('/admin/llm/test-model', 'POST', { provider_id: newProvider.id, model: 'kimi-k3[1M]', capability: 'chat' });
    assert.equal(probe.ok, false);
    assert(probe.error.includes('LLM_PROVIDER_PROTOCOL_MISMATCH'));
    await page.goto(`${webOrigin}/e2e/llm-usage.html`);
    await page.getByText('返回：glm-5.3-flash', { exact: true }).waitFor();
    await page.getByText('请求：kimi-k3[1M]', { exact: true }).first().waitFor();
    await page.getByText('响应协议不匹配', { exact: true }).waitFor();
    await page.screenshot({ path: join(artifacts, 'request-response-provenance.png'), fullPage: true });
    console.log(JSON.stringify({ ok: true, seen, persisted: routes[0].model, artifacts }));
  } finally {
    await browser?.close();
    for (const child of children) child.kill('SIGTERM');
    upstream.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
