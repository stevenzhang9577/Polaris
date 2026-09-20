import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { PythonRuntimeManager } from './main/python-runtime-manager';
import { inspectPython, pythonEnvironment, validateSelection } from './main/python-environment';
import { bootstrapEngine, type EngineCommand } from './main/engine-bootstrap';

const managed = { mode: 'managed' as const, pathDirectories: [] };
const command: EngineCommand = { mode: 'command', command: ['/fake/python'], port: 18080 };
const candidate = { executable: '/fake/python', version: '3.12.9', architecture: process.arch, implementation: 'CPython', compatible: true };
const ops = { bootstrap: async () => command, inspect: async () => candidate, validate: async () => undefined };
async function until(predicate: () => boolean) {
  for (let i = 0; i < 300 && !predicate(); i++) await delay(10);
  assert(predicate(), 'state transition timed out');
}
function directory(t: { after(fn: () => void): void }) {
  const dir = mkdtempSync(join(tmpdir(), 'polaris-python-test-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}
test('fresh startup asks for a selection without installing anything', async (t) => {
  const manager = new PythonRuntimeManager(directory(t), '/resources', { drain: async () => 0, activate: async () => undefined }, ops);
  assert.equal(await manager.boot(undefined), null);
  assert.equal(manager.getStatus().current, null);
});
test('prepare waits for jobs, applies once, persists and reuses on restart', async (t) => {
  const dir = directory(t);
  let jobs = 2, activations = 0, released = false;
  const manager = new PythonRuntimeManager(dir, '/resources', {
    drain: async (paused) => { released = !paused; return jobs; }, activate: async () => { activations++; },
  }, ops);
  manager.prepare(managed);
  await until(() => manager.getStatus().phase === 'waiting');
  assert.equal(activations, 0);
  jobs = 0;
  await until(() => manager.getStatus().phase === 'ready');
  assert.equal(activations, 1);
  assert(released);
  const stored = JSON.parse(readFileSync(join(dir, 'engine/python-environment.json'), 'utf8'));
  assert(stored.current.directory.startsWith(join(dir, 'engine/runtimes')));
  assert.equal(stored.pending, null);
  const restarted = new PythonRuntimeManager(dir, '/resources', { drain: async () => 0, activate: async () => undefined }, ops);
  assert.deepEqual(await restarted.boot(undefined), command);
});
test('cancel while draining releases barrier without activation', async (t) => {
  let released = false;
  const manager = new PythonRuntimeManager(directory(t), '/resources', {
    drain: async (paused) => { released = !paused; return 1; }, activate: async () => { assert.fail('cancelled runtime activated'); },
  }, ops);
  manager.prepare(managed);
  await until(() => manager.getStatus().phase === 'waiting');
  manager.cancel();
  await until(() => manager.getStatus().phase === 'cancelled');
  assert(released);
  assert.equal(manager.getStatus().current, null);
});
test('failed health check restores legacy runtime and reconnects client', async (t) => {
  const dir = directory(t);
  mkdirSync(join(dir, 'engine'));
  writeFileSync(join(dir, 'engine/bootstrap.json'), '{}');
  let attempts = 0;
  const manager = new PythonRuntimeManager(dir, '/resources', {
    drain: async () => 0, activate: async () => { if (++attempts === 1) throw new Error('health failed'); },
  }, ops);
  manager.prepare({ mode: 'local', executable: '/chosen/python', pathDirectories: [] });
  await until(() => manager.getStatus().phase === 'failed');
  assert.equal(attempts, 2);
  assert.equal(manager.getStatus().current?.mode, 'managed');
  assert.equal(manager.getStatus().generation, 1);
});
test('install failure never stops old backend; configuration write failure is explicit', async (t) => {
  const dir = directory(t);
  const manager = new PythonRuntimeManager(dir, '/resources', {
    drain: async () => { assert.fail('drained before successful validation'); }, activate: async () => undefined,
  }, { ...ops, bootstrap: async () => { throw new Error('installation failed'); } });
  manager.prepare(managed);
  await until(() => manager.getStatus().phase === 'failed');
  mkdirSync(join(dir, 'engine/python-environment.json.tmp'));
  assert.throws(() => manager.prepare(managed));
});
test('corrupt configuration exposes recovery instead of breaking host IPC', async (t) => {
  const dir = directory(t);
  mkdirSync(join(dir, 'engine'));
  writeFileSync(join(dir, 'engine/python-environment.json'), '{broken');
  const manager = new PythonRuntimeManager(dir, '/resources', { drain: async () => 0, activate: async () => undefined }, ops);
  assert.equal(manager.getStatus().phase, 'failed');
  assert.equal(await manager.boot(undefined), null);
});
test('local mode isolates environment, forbids interpreter downloads and rejects relative paths', () => {
  const env = pythonEnvironment({ mode: 'local', executable: '/中文 路径/python', pathDirectories: ['/extra/bin'] }, '/polaris/engine');
  assert.equal(env.UV_PYTHON_DOWNLOADS, 'never');
  assert.equal(env.PYTHONHOME, undefined);
  assert.equal(env.PYTHONPATH, undefined);
  assert.equal(env.VIRTUAL_ENV, undefined);
  assert(env.PATH?.startsWith('/extra/bin'));
  assert.equal(env.UV_PYTHON_BIN_DIR, '/polaris/engine/bin');
  assert.throws(() => validateSelection({ mode: 'local', executable: 'python', pathDirectories: [] }));
});
test('probe validates version/architecture, timeout, missing path and Unicode spaces', async (t) => {
  const dir = directory(t);
  const file = join(dir, '中文 python');
  const fixture = (data: object) => writeFileSync(file, '#!/bin/sh\nprintf \'%s\\n\' \'' + JSON.stringify({ ...candidate, executable: file, ...data }) + "'\n", { mode: 0o755 });
  fixture({});
  assert.equal((await inspectPython(file)).compatible, true);
  fixture({ version: '3.9.1' });
  assert.equal((await inspectPython(file)).compatible, false);
  fixture({ architecture: process.arch === 'arm64' ? 'x86_64' : 'arm64' });
  assert.equal((await inspectPython(file)).compatible, false);
  assert.equal((await inspectPython(join(dir, 'missing'))).compatible, false);
  writeFileSync(file, '#!/bin/sh\nexec sleep 7\n');
  assert.equal((await inspectPython(file)).compatible, false);
});
test('source-only updates reuse cached environment without invoking uv', async (t) => {
  const dir = directory(t), resources = join(dir, 'resources'), engine = join(dir, 'engine');
  mkdirSync(join(resources, 'uv'), { recursive: true });
  mkdirSync(join(resources, 'backend'));
  mkdirSync(join(engine, 'venv/bin'), { recursive: true });
  writeFileSync(join(resources, 'uv/uv'), '#!/bin/sh\nexit 99\n', { mode: 0o755 });
  writeFileSync(join(resources, 'uv/version.txt'), 'test');
  writeFileSync(join(resources, 'backend/pyproject.toml'), '[project]\n');
  writeFileSync(join(resources, 'backend/.hash'), 'new-source');
  writeFileSync(join(engine, 'venv/bin/python'), 'placeholder');
  const { createHash } = await import('node:crypto');
  writeFileSync(join(engine, 'bootstrap.json'), JSON.stringify({ pythonVersion: '3.12', interpreter: 'managed:3.12', backendHash: 'old-source', dependencyHash: createHash('sha256').update('[project]\n').digest('hex') }));
  const result = await bootstrapEngine({ dataDir: dir, resourcesDir: resources });
  assert.equal(result.command[0], join(engine, 'venv/bin/python'));
});
test('dependency installation builds in writable runtime copy, never sealed App resources', async (t) => {
  const dir = directory(t), resources = join(dir, 'resources');
  mkdirSync(join(resources, 'uv'), { recursive: true });
  mkdirSync(join(resources, 'backend'));
  writeFileSync(join(resources, 'uv/uv'), '#!/bin/sh\nif [ "$1" = "venv" ]; then\n mkdir -p "$4/bin"\n touch "$4/bin/python"\nelif [ "$1" = "pip" ]; then\n touch "$5/build-marker"\nfi\n', { mode: 0o755 });
  writeFileSync(join(resources, 'uv/version.txt'), 'test');
  writeFileSync(join(resources, 'backend/pyproject.toml'), '[project]\n');
  writeFileSync(join(resources, 'backend/.hash'), 'source');
  await bootstrapEngine({ dataDir: dir, resourcesDir: resources });
  assert(!existsSync(join(resources, 'backend/build-marker')));
  assert(!existsSync(join(dir, 'engine/install-source')));
  assert(existsSync(join(dir, 'engine/bootstrap.json')));
});
