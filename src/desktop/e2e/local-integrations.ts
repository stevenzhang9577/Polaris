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
import { launchTestBrowser } from './test-browser';

function originalPdfFixture(): Buffer {
  const stream = 'BT /F1 18 Tf 50 200 Td (Original Zotero PDF) Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
  ];
  let body = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((value, index) => { offsets.push(body.length); body += `${index + 1} 0 obj\n${value}\nendobj\n`; });
  const start = body.length;
  body += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(value => `${String(value).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(body);
}

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
    browser = await launchTestBrowser();
    const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      let phase = 'idle';
      const candidate = { executable: '/local/中文 Python/python3.12', version: '3.12.9', architecture: 'arm64', implementation: 'CPython', compatible: true };
      const settingPreview = new URLSearchParams(location.search).get('view') === 'settings';
      const localPython = { ...candidate, version: '3.14.7', executable: '/opt/homebrew/Caskroom/miniconda/base/bin/python3.14' };
      Object.assign(window, {
        __POLARIS__: { serverUrl: location.origin, platform: 'darwin', appVersion: 'test' },
        polaris: {
          invoke: async (method: string) => {
            if (method === 'host.python.status') return { current: settingPreview ? { mode: 'local', executable: localPython.executable, pathDirectories: [] } : null, currentPython: settingPreview ? localPython : undefined, pending: null, effectivePath: ['/local/bin'], jobId: phase === 'idle' ? null : 'test', phase: settingPreview && phase === 'idle' ? 'ready' : phase, generation: 0, activeTasks: 2 };
            if (method === 'host.python.detect') return settingPreview ? [candidate, localPython, { ...candidate, executable: '/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14', version: '3.14.7' }, { ...candidate, executable: '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9', version: '3.9.6', compatible: false, reason: 'Requires CPython >=3.12 (arm64)' }] : [candidate];
            if (method === 'host.python.validate') return candidate;
            if (method === 'host.python.prepare') { phase = 'waiting'; return { jobId: 'test' }; }
            if (method === 'host.python.cancel') { phase = 'cancelled'; return {}; }
            return method === 'kernel.localBackend'
            ? { baseUrl: location.origin }
            : { contract: 5, hostVersion: 'test', platform: 'darwin', capabilities: {
              'obsidian.vault.sync': { available: true },
              'python.environment.manage': { available: true },
              'plugins.manage': { available: true },
            } }; },
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
    let importCount = 0;
    let importRequestId = '';
    let originalReads = 0;
    let pdfCopies = 0;
    let managedDirectory = 'Polaris';
    let directoryChanges = 0;
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
      if (url.pathname.endsWith('/zotero-local-pdf')) {
        originalReads += 1;
        return route.fulfill({ status: 200, contentType: 'application/pdf', body: originalPdfFixture() });
      }
      if (url.pathname.endsWith('/zotero-local-materialize')) { pdfCopies += 1; return reply({}); }
      if (url.pathname.endsWith('/assets')) return reply({ items: [] });
      if (url.pathname.endsWith('/content-version')) return reply({ detail: 'NOT_FOUND' }, 404);
      if (url.pathname.endsWith('/users/me')) return reply({ id: 'user-test', email: 'test@example.test' });
      if (url.pathname.endsWith('/zotero-local/probe')) return reply({ available: true });
      if (url.pathname.endsWith('/zotero-local/collections')) return reply([
        { key: 'ROOT', name: 'Research', parent_key: null },
        { key: 'CHILD', name: 'Agents', parent_key: 'ROOT' },
      ]);
      if (url.pathname.endsWith('/zotero-local/bindings') || url.pathname.endsWith('/disciplines')) return reply([]);
      if (url.pathname.endsWith('/zotero-local/import-library')) {
        const body = route.request().postDataJSON();
        assert.equal(body.collection_key, 'CHILD');
        assert.equal(body.name, 'Agents');
        if (++importCount === 1) { importRequestId = body.request_id; return reply({ detail: 'temporary error' }, 503); }
        assert.equal(body.request_id, importRequestId);
        return reply({ library_id: 'imported', binding_id: 'binding-test', run_id: 'run-test', dispatch_pending: false }, 202);
      }
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
      if (url.pathname.endsWith('/obsidian-vault')) {
        if (route.request().method() === 'PUT') {
          const body = route.request().postDataJSON() as { vault_path: string; managed_directory: string };
          assert.equal(body.vault_path, '/synthetic/Research');
          assert.equal(body.managed_directory, '008-Polaris');
          managedDirectory = body.managed_directory;
          directoryChanges += 1;
        }
        return reply({
        connection: { id: 'vault-test', vault_path: '/synthetic/Research', watching: true,
          managed_directory: managedDirectory, status: 'ready', last_synced_at: revision.created_at }, bindings: [], conflict_count: resolved ? 0 : 1,
        });
      }
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
    const folder = page.getByRole('textbox', { name: 'Vault 内的同步目录' });
    assert.equal(await folder.inputValue(), 'Polaris');
    await folder.fill('../private');
    assert(await page.getByRole('button', { name: '保存目录', exact: true }).isDisabled());
    await page.getByRole('alert').filter({ hasText: '请输入有效目录名' }).waitFor();
    await folder.fill('008-Polaris');
    await page.getByText('/synthetic/Research/008-Polaris/', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存目录', exact: true }).click();
    await page.getByRole('button', { name: '确认更换', exact: true }).click();
    await page.getByText('同步目录已更新，文件与冲突记录已保留', { exact: true }).waitFor();
    assert.equal(directoryChanges, 1);
    await page.reload();
    await page.getByRole('textbox', { name: 'Vault 内的同步目录' }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: 'Vault 内的同步目录' }).inputValue(), '008-Polaris');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: join(artifacts, 'vault-custom-folder-mobile.png'), fullPage: true });
    console.log('PASS: Vault folder validation, confirmed update, persistence and narrow layout');
    await page.goto(`${origin}/e2e/local-integrations.html?view=libraries`);
    await page.getByText('还没有文献库', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).count(), 2);
    await page.screenshot({ path: join(artifacts, 'zotero-entry-mobile.png'), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).last().click();
    await page.getByLabel('搜索 Collection').fill('Research / Agents');
    assert(await page.getByRole('button', { name: '创建并同步', exact: true }).isDisabled());
    await page.getByRole('radio', { name: 'Research / Agents', exact: true }).check();
    await page.screenshot({ path: join(artifacts, 'zotero-import-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '创建并同步', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '可重试' }).waitFor();
    await page.getByRole('button', { name: '创建并同步', exact: true }).click();
    await page.getByText('Imported library destination').waitFor();
    assert.equal(importCount, 2);
    console.log('PASS: two Zotero entrypoints, narrow layout, explicit Collection choice, retry identity and navigation');
    await page.goto(`${origin}/e2e/local-integrations.html?view=python`);
    await page.getByRole('radio').nth(1).waitFor();
    assert(await page.getByRole('button', { name: '保存并应用' }).isDisabled());
    await page.getByRole('radio').nth(1).check();
    await page.getByRole('button', { name: '保存并应用' }).click();
    await page.getByText('环境已就绪，等待当前任务完成 (2)', { exact: true }).waitFor();
    await page.screenshot({ path: join(artifacts, 'python-waiting-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '取消准备' }).click();
    await page.getByText('已取消', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: first-run explicit Python choice, waiting and cancellation UI');
    await page.goto(`${origin}/e2e/local-integrations.html?view=settings`);
    await page.getByRole('heading', { name: '选择解释器', exact: true }).waitFor();
    for (const width of [1100, 820, 430]) {
      await page.setViewportSize({ width, height: 900 });
      const layout = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth > innerWidth,
        labels: Array.from(document.querySelectorAll('.settings-navigation button')).map((node) => ({
          nowrap: getComputedStyle(node).whiteSpace,
          height: node.getBoundingClientRect().height,
        })),
        font: getComputedStyle(document.querySelector('.python-environment__option strong')!).fontSize,
      }));
      assert(!layout.overflow, `page overflow at ${width}px`);
      assert(layout.labels.length >= 18);
      assert(layout.labels.every((label) => label.nowrap === 'nowrap' && label.height <= 38));
      assert.equal(layout.font, '13px');
      await page.screenshot({ path: join(artifacts, `settings-python-${width}.png`), fullPage: true });
    }
    await page.getByRole('radio').first().focus();
    await page.keyboard.press('Space');
    assert(await page.getByRole('radio').first().isChecked());
    assert(await page.getByRole('radio').last().isDisabled());
    assert.deepEqual(errors, []);
    console.log('PASS: responsive settings navigation, compact Python typography and keyboard selection');
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto(`${origin}/e2e/local-integrations.html?view=reader`);
    await page.locator('.react-pdf__Page__canvas').first().waitFor({ state: 'visible' });
    assert(originalReads > 0, 'reader automatically opens the Zotero original');
    assert.equal(pdfCopies, 0, 'reading must not call the legacy materialize endpoint');
    await page.screenshot({ path: join(artifacts, 'zotero-original-reader.png'), fullPage: true });
    console.log('PASS: original Zotero PDF renders automatically without a copy or extra click');
    console.log(`Screenshots: ${artifacts}`);
  } finally {
    await browser?.close();
    server.kill('SIGTERM');
    if (server.exitCode === null) await once(server, 'exit');
  }
}

main().catch((error: unknown) => { console.error(error); process.exitCode = 1; });
