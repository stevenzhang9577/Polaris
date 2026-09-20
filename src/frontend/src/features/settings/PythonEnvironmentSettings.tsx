import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { pythonHost, type PythonCandidate, type PythonSelection } from '../../lib/host';
import { tr } from '../../lib/i18n';
import './python-environment.css';

function pythonSource(path: string) {
  const value = path.toLowerCase();
  if (value.includes('conda') || value.includes('miniforge')) return 'Conda';
  if (value.includes('homebrew') || value.includes('/cellar/')) return 'Homebrew';
  if (value.includes('.pyenv')) return 'pyenv';
  if (value.includes('/uv/python/')) return 'uv';
  if (value.includes('commandlinetools') || value.includes('/xcode.')) return 'Xcode';
  return tr('本机 Python', 'Local Python');
}

export function PythonEnvironmentSettings() {
  const status = useQuery({ queryKey: ['python-environment'], queryFn: pythonHost.status, refetchInterval: 1000, retry: false });
  const [selection, setSelection] = useState<PythonSelection | null>(null);
  const [paths, setPaths] = useState<string | null>(null);
  const [custom, setCustom] = useState('');
  const [tested, setTested] = useState<PythonCandidate | null>(null);
  const dirs = (paths ?? status.data?.current?.pathDirectories.join('\n') ?? '').split('\n').map((s) => s.trim()).filter(Boolean);
  const candidates = useQuery({ queryKey: ['python-candidates'], queryFn: () => pythonHost.detect(dirs), retry: false, staleTime: Infinity });
  const selected = selection ?? status.data?.current ?? null;
  const generation = useRef<number | null>(null);
  useEffect(() => {
    if (!status.data) return;
    if (generation.current === null) generation.current = status.data.generation;
    else if (generation.current !== status.data.generation) window.location.reload();
  }, [status.data]);
  const validate = useMutation({ mutationFn: pythonHost.validate, onSuccess: (c) => { setTested(c); setSelection({ mode: 'local', executable: c.executable, pathDirectories: dirs }); } });
  const pick = useMutation({ mutationFn: pythonHost.pick, onSuccess: ({ path }) => { if (path) { setCustom(path); validate.mutate(path); } } });
  const prepare = useMutation({ mutationFn: () => pythonHost.prepare({ ...selected!, pathDirectories: dirs }), onSuccess: () => void status.refetch() });
  const cancel = useMutation({ mutationFn: pythonHost.cancel, onSuccess: () => void status.refetch() });
  const busy = !!status.data?.jobId && !['idle', 'ready', 'failed', 'cancelled'].includes(status.data.phase);
  const compatible = selected?.mode === 'managed' || selected?.executable === status.data?.current?.executable
    || [tested, ...(candidates.data ?? [])].some((c) => c?.executable === selected?.executable && c?.compatible);
  const labels: Record<string, string> = {
    detect: tr('正在检测解释器', 'Checking interpreter'), python: tr('准备 Python', 'Preparing Python'),
    venv: tr('创建独立环境', 'Creating isolated environment'), install: tr('安装后端依赖', 'Installing dependencies'),
    validate: tr('检查后端兼容性', 'Checking backend compatibility'), waiting: tr('环境已就绪，等待当前任务完成', 'Environment ready; waiting for active tasks'),
    switching: tr('正在切换并重新连接…', 'Switching and reconnecting…'), ready: tr('环境已就绪', 'Environment ready'),
    failed: tr('环境准备失败', 'Environment setup failed'), cancelled: tr('已取消', 'Cancelled'), idle: tr('请选择运行环境', 'Choose a runtime'),
  };
  const current = status.data?.current;
  const currentPython = status.data?.currentPython;
  const currentPath = currentPython?.executable ?? current?.executable;
  return (
    <section className="card python-environment">
      <header className="python-environment__header">
        <h2>{tr('本地运行环境', 'Local runtime')}</h2>
        <p>{tr('使用本机 Python 3.12+，或由 Polaris 管理运行环境。独立安装，不修改系统配置。', 'Use local Python 3.12+ or a Polaris-managed runtime. Isolated installation; no system changes.')}</p>
      </header>

      {current && <div className="python-environment__current">
        <div className="python-environment__heading">
          <span className="python-environment__eyebrow">{tr('当前环境', 'Current runtime')}</span>
          <span className="python-environment__badge">{current.mode === 'managed' ? tr('Polaris 托管', 'Polaris managed') : pythonSource(current.executable ?? '')}</span>
          {currentPython && <strong>Python {currentPython.version}<span className="python-environment__meta"> · {currentPython.architecture}</span></strong>}
        </div>
        {currentPath && <code className="python-environment__path" title={currentPath}>{currentPath}</code>}
      </div>}

      <div className="python-environment__heading">
        <h3>{tr('选择解释器', 'Choose an interpreter')}</h3>
        <span className="python-environment__meta">{tr('首次安装依赖需要联网', 'Internet required for initial dependencies')}</span>
      </div>
      <div role="radiogroup" aria-label={tr('Python 环境', 'Python runtime')} className="python-environment__options">
        <label className="python-environment__option" data-selected={selected?.mode === 'managed'} data-disabled={busy}>
          <input type="radio" name="python-environment" checked={selected?.mode === 'managed'} disabled={busy} onChange={() => setSelection({ mode: 'managed', pathDirectories: dirs })} />
          <span className="python-environment__option-body">
            <span className="python-environment__heading"><strong>Polaris Python 3.12</strong><span className="python-environment__badge">{tr('托管', 'Managed')}</span></span>
            <span className="python-environment__meta">{tr('按需下载，由 Polaris 独立维护', 'Download on demand, maintained by Polaris')}</span>
          </span>
        </label>
        {(candidates.data ?? []).map((c) => (
          <label key={c.executable} className="python-environment__option" data-selected={selected?.mode === 'local' && selected.executable === c.executable} data-disabled={busy || !c.compatible}>
            <input type="radio" name="python-environment" disabled={busy || !c.compatible} checked={selected?.mode === 'local' && selected.executable === c.executable} onChange={() => setSelection({ mode: 'local', executable: c.executable, pathDirectories: dirs })} />
            <span className="python-environment__option-body">
              <span className="python-environment__heading"><strong>Python {c.version || '?'}</strong><span className="python-environment__meta">{c.architecture}</span><span className="python-environment__badge">{pythonSource(c.executable)}</span></span>
              <code className="python-environment__path" title={c.executable}>{c.executable}</code>
              {c.reason && <span className="python-environment__meta">{c.reason}</span>}
            </span>
          </label>
        ))}
      </div>
      {candidates.isFetching && <span className="python-environment__meta">{tr('正在检测本机 Python…', 'Detecting local Python…')}</span>}

      <div className="row gap8 wrap">
        <button className="btn btn-soft sm" disabled={busy || candidates.isFetching} onClick={() => void candidates.refetch()}>{tr('重新检测', 'Detect again')}</button>
        <button className="btn btn-soft sm" disabled={busy || pick.isPending} onClick={() => pick.mutate()}>{tr('选择解释器文件', 'Choose executable')}</button>
      </div>
      <div className="python-environment__custom">
        <input className="input" aria-label={tr('Python 绝对路径', 'Python absolute path')} placeholder={tr('或输入解释器的完整路径', 'Or enter the full interpreter path')} value={custom} disabled={busy} onChange={(e) => { setCustom(e.target.value); setTested(null); }} />
        <button className="btn btn-soft sm" disabled={busy || !custom || validate.isPending} onClick={() => validate.mutate(custom)}>{tr('测试环境', 'Test environment')}</button>
      </div>
      {tested && <div role="status" className="python-environment__meta">Python {tested.version} · {tested.architecture} · {tested.compatible ? tr('可用于创建 Polaris 环境', 'Ready to create Polaris environment') : tested.reason}</div>}
      <details className="python-environment__advanced">
        <summary>{tr('高级：Polaris 专用 PATH', 'Advanced: Polaris PATH')}</summary>
        <textarea className="textarea" rows={3} aria-label="PATH" disabled={busy} placeholder={tr('每行一个绝对目录', 'One absolute directory per line')} value={paths ?? current?.pathDirectories.join('\n') ?? ''} onChange={(e) => setPaths(e.target.value)} />
        <span className="python-environment__meta">{tr('当前生效 PATH', 'Current effective PATH')}</span>
        <code className="python-environment__full-path">{status.data?.effectivePath.join('\n')}</code>
      </details>
      {status.data?.pending && <div className="python-environment__meta">{tr('正在准备', 'Preparing')}：<span className="python-environment__full-path">{status.data.pending.mode === 'managed' ? 'Polaris Python 3.12' : status.data.pending.executable}</span></div>}
      {status.data?.message && <p role="alert" className="python-environment__notice">{status.data.message}</p>}
      {[status.error, candidates.error, validate.error, pick.error, prepare.error, cancel.error].filter(Boolean).map((e, i) => <p key={i} role="alert" className="python-environment__error">{e?.message}</p>)}
      <footer className="python-environment__footer">
        <div role="status" className="python-environment__meta">{labels[status.data?.phase ?? 'idle']}{status.data?.phase === 'waiting' && ` (${status.data.activeTasks ?? 0})`}</div>
        <div className="row gap8 wrap">
          {busy && <button className="btn btn-ghost sm" disabled={status.data?.phase === 'switching' || cancel.isPending} onClick={() => cancel.mutate()}>{tr('取消准备', 'Cancel preparation')}</button>}
          <button className="btn btn-primary sm" disabled={!selected || !compatible || busy || prepare.isPending || status.isError} onClick={() => prepare.mutate()}>{tr('保存并应用', 'Save and apply')}</button>
        </div>
      </footer>
    </section>
  );
}
