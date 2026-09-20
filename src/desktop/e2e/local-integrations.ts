/** Real-browser regressions for the shared React UI, with isolated synthetic API/host fixtures.
 * Run: pnpm --dir src/desktop run e2e:local-integrations
 * POLARIS_TEST_BROWSER optionally selects a Chromium executable; no user profile is reused.
 */
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
  const port = address.port;
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  const origin = `http://127.0.0.1:${port}`;
  const server = spawn(process.execPath, [join(frontend, 'node_modules/vite/bin/vite.js'),
    '--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: frontend, stdio: 'pipe' });
  let output = '';
  server.stdout?.on('data', (chunk) => { output += String(chunk); });
  server.stderr?.on('data', (chunk) => { output += String(chunk); });
  const artifacts = mkdtempSync(join(tmpdir(), 'polaris-ui-regressions-'));
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
    browser = await chromium.launch({
      executablePath: process.env.POLARIS_TEST_BROWSER ?? (
        process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
          : process.platform === 'win32' ? 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
            : '/usr/bin/chromium'
      ),
      headless: true,
    });
    const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: {
          invoke: async (method: string) => method === 'kernel.localBackend'
            ? { baseUrl: location.origin }
            : { contract: 4, hostVersion: 'test', platform: 'darwin', capabilities: {
              'obsidian.vault.sync': { available: true },
            } },
          subscribe: () => 1, unsubscribe: () => undefined,
        },
      });
    });
    const revision = {
      id: 'revision-new', paper_id: 'paper-test', content_version_id: 'content-1',
      source_level: 'fulltext', content: '## TL;DR\nTest summary', tldr: 'Test summary',
      model: 'test-model', status: 'ready', stage: 'complete', is_current: true,
      created_at: '2026-09-20T00:00:00Z', updated_at: '2026-09-20T00:00:00Z',
    };
    let current = revision;
    let deleted = false;
    let generatedLibrary: string | null = null;
    let resolved = false;
    let resolveCount = 0;
    const conflict = {
      id: 'conflict-1', version: 'a'.repeat(64), entity_type: 'summary', entity_id: 'paper-test',
      relative_path: 'research/papers/example.md', base_content: 'base',
      polaris_content: 'Polaris change', vault_content: 'Vault change', status: 'open',
      resolution: null, resolved_at: null, created_at: revision.created_at, updated_at: revision.updated_at,
    };
    await page.route('**/api/**', async (route) => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      const reply = (body: unknown, status = 200) => route.fulfill({
        status, contentType: 'application/json', body: JSON.stringify(body),
      });
      if (url.pathname.endsWith('/summary')) {
        return deleted ? reply({ detail: 'SUMMARY_NOT_FOUND' }, 404)
          : reply({ paper_id: 'paper-test', current_revision: current, stale: false });
      }
      if (url.pathname.endsWith('/summaries')) {
        if (method === 'POST') {
          generatedLibrary = url.searchParams.get('library_id');
          return reply({ paper_id: 'paper-test', revision_id: 'queued', status: 'queued', stage: 'materialize' }, 202);
        }
        return reply([{ ...revision, is_current: current.id === revision.id },
          ...(current.id !== revision.id ? [current] : [])]);
      }
      if (url.pathname.endsWith('/conflicts')) return reply(resolved ? [] : [conflict]);
      if (url.pathname.endsWith('/resolve')) {
        const body = route.request().postDataJSON() as { expected_version: string; content: string };
        resolveCount += 1;
        assert.equal(body.content, 'My retained merge draft');
        if (resolveCount === 1) {
          assert.equal(body.expected_version, 'c'.repeat(64));
          conflict.version = 'b'.repeat(64);
          conflict.vault_content = 'Newer Vault edit';
          return reply({ detail: 'OBSIDIAN_CONFLICT_CHANGED' }, 409);
        }
        assert.equal(body.expected_version, 'b'.repeat(64));
        resolved = true;
        return reply({ ...conflict, status: 'resolved' });
      }
      if (url.pathname.endsWith('/obsidian-vault')) return reply({
        connection: { id: 'vault-test', vault_path: '/synthetic/Research', watching: true,
          status: 'ready', last_synced_at: revision.created_at }, bindings: [], conflict_count: resolved ? 0 : 1,
      });
      if (url.pathname.endsWith('/libraries')) return reply([]);
      return reply({ detail: 'UNEXPECTED_TEST_REQUEST' }, 404);
    });
    await page.goto(`${origin}/e2e/local-integrations.html`);
    await page.getByText('全文级', { exact: true }).waitFor();
    await page.getByRole('button', { name: '重新生成', exact: true }).click();
    await page.getByText('总结任务已排队', { exact: true }).waitFor();
    assert.equal(generatedLibrary, 'library-zotero');
    // External activation keeps the newest ready history ID unchanged.
    current = { ...revision, id: 'revision-old', source_level: 'abstract' };
    await page.getByText('摘要级', { exact: true }).waitFor({ timeout: 12_000 });
    deleted = true;
    await page.getByRole('button', { name: '恢复', exact: true }).waitFor({ timeout: 12_000 });
    assert.equal(await page.getByText('摘要级', { exact: true }).count(), 0);
    await page.screenshot({ path: join(artifacts, 'summary-trash.png'), fullPage: true });
    console.log('PASS: current Library, external activation, external deletion');

    await page.goto(`${origin}/e2e/local-integrations.html?view=vault`);
    await page.getByRole('button', { name: '解决', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑合并结果' }).fill('My retained merge draft');
    conflict.version = 'c'.repeat(64);
    conflict.vault_content = 'Edit discovered by polling';
    await page.getByRole('button', { name: '刷新版本，保留草稿', exact: true }).waitFor({ timeout: 10_000 });
    assert(await page.getByRole('button', { name: '保存合并结果', exact: true }).isDisabled());
    await page.getByRole('button', { name: '刷新版本，保留草稿', exact: true }).click();
    assert.equal(await page.getByRole('textbox', { name: '编辑合并结果' }).inputValue(), 'My retained merge draft');
    await page.getByRole('button', { name: '保存合并结果', exact: true }).click();
    await page.getByText('Newer Vault edit', { exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '编辑合并结果' }).inputValue(), 'My retained merge draft');
    await page.setViewportSize({ width: 430, height: 900 });
    await page.screenshot({ path: join(artifacts, 'conflict-draft.png'), fullPage: true });
    await page.getByRole('button', { name: '保存合并结果', exact: true }).click();
    await page.getByText('没有待解决冲突。', { exact: true }).waitFor();
    assert.equal(resolveCount, 2);
    assert.deepEqual(errors, []);
    console.log('PASS: stale conflict rejected, draft retained, refreshed version submitted');
    console.log(`Screenshots: ${artifacts}`);
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
}

main().catch((error: unknown) => { console.error(error); process.exitCode = 1; });
