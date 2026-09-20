/** Python discovery is independent of Electron, shells and the running backend. */
import { execFile } from 'node:child_process';
import { existsSync, readdirSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, dirname, isAbsolute, join } from 'node:path';
import { promisify } from 'node:util';
import type { PythonCandidate, PythonSelection } from '../shared/python-environment';

const exec = promisify(execFile);
export function validateSelection(value: unknown): PythonSelection {
  const s = value as PythonSelection;
  if (!s || !['managed', 'local'].includes(s.mode) || !Array.isArray(s.pathDirectories)
      || s.pathDirectories.length > 20 || s.pathDirectories.some((p) => typeof p !== 'string' || !isAbsolute(p) || p.includes('\0') || p.includes(delimiter))) throw new Error('PYTHON_INVALID_CONFIG');
  if (s.mode === 'local' && (!s.executable || !isAbsolute(s.executable) || s.executable.includes('\0'))) throw new Error('PYTHON_ABSOLUTE_PATH_REQUIRED');
  return { mode: s.mode, executable: s.mode === 'local' ? s.executable : undefined, pathDirectories: [...new Set(s.pathDirectories)] };
}

export function pythonEnvironment(selection: PythonSelection, engineDir: string): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key.startsWith('UV_') || key.startsWith('PYTHON') || ['VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV'].includes(key)) delete env[key];
  }
  env.PATH = [...selection.pathDirectories, ...(selection.executable ? [dirname(selection.executable)] : []), process.env.PATH ?? ''].join(delimiter);
  return { ...env, UV_PYTHON_INSTALL_DIR: join(engineDir, 'python'), UV_PYTHON_BIN_DIR: join(engineDir, 'bin'), UV_CACHE_DIR: join(engineDir, 'uv-cache'), UV_NO_CONFIG: '1',
    UV_PYTHON_DOWNLOADS: selection.mode === 'local' ? 'never' : 'automatic', UV_PYTHON_PREFERENCE: selection.mode === 'local' ? 'only-system' : 'only-managed', PYTHONUTF8: '1' };
}

export async function inspectPython(executable: string): Promise<PythonCandidate> {
  const result: PythonCandidate = { executable, version: '', architecture: '', implementation: '', compatible: false };
  try {
    if (!isAbsolute(executable) || !existsSync(executable)) throw new Error('Python executable does not exist');
    const { stdout } = await exec(executable, ['-I', '-c', 'import json,sys,platform; print(json.dumps(dict(executable=sys.executable,version=platform.python_version(),architecture=platform.machine(),implementation=platform.python_implementation())))'], {
      timeout: 5000, maxBuffer: 8192, env: pythonEnvironment({ mode: 'local', executable, pathDirectories: [] }, dirname(executable)),
    });
    Object.assign(result, JSON.parse(stdout.trim()));
    result.executable = realpathSync(result.executable);
    const [major, minor] = result.version.split('.').map(Number);
    const arch = result.architecture.toLowerCase().replace('aarch64', 'arm64').replace('amd64', 'x64').replace('x86_64', 'x64');
    result.compatible = result.implementation === 'CPython' && major === 3 && minor >= 12 && arch === process.arch;
    if (!result.compatible) result.reason = `Requires CPython >=3.12 (${process.arch})`;
  } catch { result.reason = 'Unable to execute Python (missing, permission denied or timed out)'; }
  return result;
}

export async function discoverPython(pathDirectories: string[] = []): Promise<PythonCandidate[]> {
  validateSelection({ mode: 'managed', pathDirectories });
  const dirs = new Set([...pathDirectories, ...(process.env.PATH ?? '').split(delimiter), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin']);
  const bases = [join(homedir(), '.pyenv/versions'), '/Library/Frameworks/Python.framework/Versions', join(homedir(), 'miniconda3/envs'), join(homedir(), 'anaconda3/envs')];
  for (const base of bases) {
    try { for (const name of readdirSync(base).slice(0, 40)) dirs.add(join(base, name, 'bin')); } catch { /* absent manager */ }
  }
  for (const base of ['miniconda3', 'anaconda3', 'miniforge3']) dirs.add(join(homedir(), base, 'bin'));
  const paths = new Set<string>();
  for (const dir of [...dirs].filter(isAbsolute).slice(0, 100)) {
    for (const name of ['python3.12', 'python3.13', 'python3.14', 'python3.15', 'python3', 'python', 'python.exe']) {
      try { paths.add(realpathSync(join(dir, name))); } catch { /* missing */ }
    }
  }
  const found: PythonCandidate[] = [];
  const pending = [...paths].slice(0, 80);
  await Promise.all(Array.from({ length: 4 }, async () => {
    for (let path = pending.shift(); path; path = pending.shift()) found.push(await inspectPython(path));
  }));
  return [...new Map(found.map((c) => [c.executable, c])).values()].sort((a, b) => Number(b.compatible) - Number(a.compatible) || Number(b.version.startsWith('3.12.')) - Number(a.version.startsWith('3.12.')) || a.executable.localeCompare(b.executable));
}
