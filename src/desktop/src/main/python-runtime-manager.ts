/** Persistent runtime selection and cancellable preparation; no Electron dependencies. */
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { delimiter, join } from 'node:path';
import { promisify } from 'node:util';
import type { PythonEnvironmentStatus, PythonSelection } from '../shared/python-environment';
import { bootstrapEngine, type EngineCommand } from './engine-bootstrap';
import { inspectPython, pythonEnvironment, validateSelection } from './python-environment';

interface Runtime { selection: PythonSelection; directory: string }
interface Config { current: Runtime | null; pending: Runtime | null }
export interface RuntimeCallbacks {
  drain(paused: boolean): Promise<number>;
  activate(command: EngineCommand): Promise<void>;
  deactivate?(): Promise<void>;
}
const operations = {
  bootstrap: bootstrapEngine,
  inspect: inspectPython,
  async validate(command: EngineCommand, resourcesDir: string, selection: PythonSelection, engineDir: string, signal: AbortSignal) {
    if (!(await inspectPython(command.command[0])).compatible) throw new Error('PYTHON_INCOMPATIBLE');
    await promisify(execFile)(command.command[0], ['-I', '-c', `import sys; sys.dont_write_bytecode=True; sys.path.insert(0, ${JSON.stringify(join(resourcesDir, 'backend'))}); from app.main import create_app; create_app()`], {
      timeout: 30000, maxBuffer: 65536, signal,
      env: { ...pythonEnvironment(selection, engineDir), POLARIS_PROFILE: 'desktop' },
    });
  },
};

export class PythonRuntimeManager {
  private config: Config;
  private controller: AbortController | null = null;
  private status: PythonEnvironmentStatus;
  private path: string;
  constructor(private dataDir: string, private resourcesDir: string, private callbacks: RuntimeCallbacks, private ops = operations) {
    this.path = join(dataDir, 'engine', 'python-environment.json');
    this.config = { current: null, pending: null };
    let configError = false;
    if (existsSync(this.path)) {
      try {
        const stored = JSON.parse(readFileSync(this.path, 'utf8')) as Config;
        this.config.current = stored.current ? { ...stored.current, selection: validateSelection(stored.current.selection) } : null;
      } catch { configError = true; }
      // Interrupted preparations are never implicitly applied on restart.
    } else if (existsSync(join(dataDir, 'engine', 'bootstrap.json'))) {
      this.config.current = { selection: { mode: 'managed', pathDirectories: [] }, directory: join(dataDir, 'engine') };
    }
    this.status = { current: this.config.current?.selection ?? null, pending: null, effectivePath: [], jobId: null, phase: 'idle', generation: 0 };
    if (configError) {
      this.status.phase = 'failed';
      this.status.message = '运行环境配置无法读取，请重新选择 Python。现有数据库和文件未被修改。';
    }
  }
  private persist(next: Config) {
    mkdirSync(join(this.dataDir, 'engine'), { recursive: true });
    const temp = this.path + '.tmp';
    writeFileSync(temp, JSON.stringify(next, null, 2), { mode: 0o600 });
    renameSync(temp, this.path);
    this.config = next;
  }
  getStatus(): PythonEnvironmentStatus {
    return { ...this.status, current: this.config.current?.selection ?? null,
      effectivePath: (pythonEnvironment(this.config.current?.selection ?? { mode: 'managed', pathDirectories: [] }, join(this.dataDir, 'engine')).PATH ?? '').split(delimiter) };
  }
  async boot(onProgress: Parameters<typeof bootstrapEngine>[0]['onProgress']): Promise<EngineCommand | null> {
    if (!this.config.current) return null;
    const command = await this.ops.bootstrap({ dataDir: this.dataDir, resourcesDir: this.resourcesDir,
      selection: this.config.current.selection, runtimeDir: this.config.current.directory, onProgress });
    this.status.currentPython = await this.ops.inspect(command.command[0]);
    if (!this.status.currentPython.compatible) throw new Error('PYTHON_INCOMPATIBLE');
    this.status.phase = 'ready';
    return command;
  }
  prepare(value: unknown): { jobId: string } {
    if (this.controller) throw new Error('PYTHON_PREPARATION_IN_PROGRESS');
    const selection = validateSelection(value);
    const id = randomUUID();
    const runtime = { selection, directory: join(this.dataDir, 'engine', 'runtimes', id) };
    this.persist({ ...this.config, pending: runtime });
    this.controller = new AbortController();
    this.status = { ...this.status, pending: selection, jobId: id, phase: 'detect', message: undefined, activeTasks: 0 };
    void this.prepareAndApply(runtime, this.controller.signal);
    return { jobId: id };
  }
  cancel() {
    if (this.status.phase === 'switching') throw new Error('PYTHON_SWITCH_IN_PROGRESS');
    this.controller?.abort();
  }
  private async prepareAndApply(runtime: Runtime, signal: AbortSignal) {
    const previous = this.config.current;
    let barrier = false;
    let switching = false;
    try {
      const command = await this.ops.bootstrap({ dataDir: this.dataDir, resourcesDir: this.resourcesDir,
        selection: runtime.selection, runtimeDir: runtime.directory, signal,
        onProgress: ({ phase }) => { this.status.phase = phase === 'check' ? 'detect' : phase === 'ready' ? 'validate' : phase; },
      });
      signal.throwIfAborted();
      this.status.phase = 'validate';
      await this.ops.validate(command, this.resourcesDir, runtime.selection, join(this.dataDir, 'engine'), signal);
      this.status.phase = 'waiting';
      barrier = true;
      while (true) {
        signal.throwIfAborted();
        const active = await this.callbacks.drain(true);
        this.status.activeTasks = active;
        if (!active) break;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      signal.throwIfAborted();
      this.status.phase = 'switching';
      switching = true;
      await this.callbacks.activate(command);
      this.persist({ current: runtime, pending: null });
      this.status.currentPython = await this.ops.inspect(command.command[0]);
      this.status.phase = 'ready';
      this.status.generation += 1;
      this.status.pending = null;
    } catch (error) {
      let restored = !switching;
      if (switching && previous) {
        try {
          const command = await this.ops.bootstrap({ dataDir: this.dataDir, resourcesDir: this.resourcesDir, selection: previous.selection, runtimeDir: previous.directory });
          await this.callbacks.activate(command);
          restored = true;
          this.status.generation += 1;
        } catch { restored = false; }
      } else if (switching) {
        await this.callbacks.deactivate?.().catch(() => undefined);
      }
      this.status.phase = signal.aborted ? 'cancelled' : 'failed';
      this.status.message = signal.aborted ? '准备已取消，当前环境保持不变。' : restored
        ? '环境准备或切换失败，原环境已保留。请检查 Python 路径、网络和依赖兼容性后重试。'
        : '新环境启动失败，原环境也未能恢复。请重新选择 Python 或重启应用。';
      try { this.persist({ current: previous, pending: null }); } catch { this.status.message += ' 配置无法写入，请检查数据目录权限。'; }
      this.status.pending = null;
      // Do not expose child output or process environment through IPC/logs.
      void error;
    } finally {
      if (barrier) await this.callbacks.drain(false).catch(() => undefined);
      this.controller = null;
    }
  }
}
