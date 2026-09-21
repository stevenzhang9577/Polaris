/** Run against the built arm64 App with an isolated profile and backend port. */
import assert from 'node:assert/strict';
import { mkdtempSync, existsSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:net';
import { once } from 'node:events';
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { _electron, type ElectronApplication } from 'playwright-core';

async function main() {
  const data = mkdtempSync(join(tmpdir(), 'polaris-packaged-setup-'));
  const socket = createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const address = socket.address();
  assert(address && typeof address !== 'string');
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  const env = Object.fromEntries(Object.entries(process.env).filter(([k, v]) => v !== undefined && !k.startsWith('POLARIS_') && k !== 'ELECTRON_RUN_AS_NODE')) as Record<string, string>;
  Object.assign(env, { POLARIS_USER_DATA_DIR: data, POLARIS_DESKTOP_ENGINE_PORT: String(address.port) });
  const executablePath = resolve('release/mac-arm64/Polaris.app/Contents/MacOS/Polaris');
  const python = resolve('../backend/.venv/bin/python');
  let app: ElectronApplication | undefined;
  async function close() {
    if (!app) return;
    const current = app;
    await current.evaluate(({ app }) => app.quit()).catch(() => undefined);
    await current.waitForEvent('close', { timeout: 30000 }).catch(() => undefined);
    app = undefined;
  }
  try {
    console.log(`Isolated profile: ${data}`);
    app = await _electron.launch({ executablePath, env, timeout: 60000 });
    let page = await app.firstWindow();
    await page.getByText('本地运行环境', { exact: true }).waitFor({ timeout: 60000 });
    assert(!existsSync(join(data, 'engine/python')));
    await page.screenshot({ path: join(data, 'first-choice.png'), fullPage: true });
    await page.getByLabel('Python 绝对路径').fill(python);
    await page.getByRole('button', { name: '测试环境', exact: true }).click();
    await page.getByText(/可用于创建 Polaris 环境/).waitFor();
    await page.getByRole('button', { name: '保存并应用', exact: true }).click();
    for (let i = 0; i < 900; i++) {
      await delay(1000);
      let status: { phase: string; message?: string };
      try { status = await page.evaluate(() => (window as any).polaris.invoke('host.python.status')); } catch { continue; }
      if (i % 15 === 0) console.log(`prepare: ${status.phase}`);
      if (status.phase === 'failed') throw new Error(status.message);
      if (status.phase === 'ready') break;
      if (i === 899) throw new Error('setup timed out');
    }
    await page.goto(new URL('/libraries', page.url()).href);
    await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).first().waitFor({ timeout: 60000 });
    assert.equal(await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).count(), 2);
    await page.screenshot({ path: join(data, 'new-library-entry.png'), fullPage: true });
    const configPath = join(data, 'engine/python-environment.json');
    const config = JSON.parse(readFileSync(configPath, 'utf8'));
    assert.equal(config.current.selection.mode, 'local');
    assert(!existsSync(join(data, 'engine/python')), 'local selection downloaded managed Python');
    const sentinel = join(config.current.directory, 'bootstrap.json');
    const modified = statSync(sentinel).mtimeMs;
    console.log('PASS: packaged first choice, local installation, session and two Zotero buttons');
    await close();
    app = await _electron.launch({ executablePath, env, timeout: 60000 });
    page = await app.firstWindow();
    await page.goto(new URL('/libraries', page.url()).href);
    await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).first().waitFor({ timeout: 120000 });
    assert.equal(statSync(sentinel).mtimeMs, modified, 'second startup rebuilt environment');
    assert(!existsSync(join(data, 'engine/python')));
    console.log('PASS: second startup reused environment without downloading Python');
    await page.goto(new URL('/settings?tab=python', page.url()).href);
    await page.getByRole('radio').first().check();
    await page.getByRole('button', { name: '保存并应用', exact: true }).click();
    for (let i = 0; i < 900; i++) {
      await delay(1000);
      let status: { phase: string; message?: string; generation: number };
      try { status = await page.evaluate(() => (window as any).polaris.invoke('host.python.status')); } catch { continue; }
      if (i % 15 === 0) console.log(`switch: ${status.phase}`);
      if (status.phase === 'failed') throw new Error(status.message);
      if (status.phase === 'ready' && status.generation > 0) break;
      if (i === 899) throw new Error('switch timed out');
    }
    await page.goto(new URL('/libraries', page.url()).href);
    await page.getByRole('button', { name: '导入 Zotero 文献库', exact: true }).first().waitFor({ timeout: 60000 });
    const switched = JSON.parse(readFileSync(configPath, 'utf8'));
    assert.equal(switched.current.selection.mode, 'managed');
    assert.notEqual(switched.current.directory, config.current.directory);
    assert(existsSync(join(config.current.directory, 'venv')), 'old environment was removed');
    console.log('PASS: settings switched local Python to managed 3.12 and reconnected successfully');
    console.log(`Screenshots and isolated profile retained: ${data}`);
  } finally { await close(); }
  execFileSync('codesign', ['--verify', '--deep', '--strict', resolve('release/mac-arm64/Polaris.app')]);
  console.log('PASS: App signature remains intact after installation, restart and environment switch');
}
main().catch((error: unknown) => { console.error(error); process.exitCode = 1; });
