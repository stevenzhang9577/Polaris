import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { hasHost, isCapabilityAvailable, CAPABILITY_PYTHON_ENVIRONMENT_MANAGE, loadCapabilities, pythonHost } from '../../lib/host';
import { tr } from '../../lib/i18n';

/** Remains mounted when users leave Settings during environment preparation. */
export function PythonRuntimeMonitor() {
  const capability = useQuery({ queryKey: ['python-capability'], queryFn: async () => { await loadCapabilities(); return isCapabilityAvailable(CAPABILITY_PYTHON_ENVIRONMENT_MANAGE); }, enabled: hasHost(), retry: false });
  const status = useQuery({ queryKey: ['python-environment'], queryFn: pythonHost.status, enabled: capability.data === true, refetchInterval: 1000, retry: false });
  const generation = useRef<number | null>(null);
  useEffect(() => {
    if (!status.data) return;
    if (generation.current === null) generation.current = status.data.generation;
    else if (generation.current !== status.data.generation) window.location.reload();
  }, [status.data]);
  if (!['waiting', 'switching'].includes(status.data?.phase ?? '')) return null;
  return <div role="status" style={{ position: 'fixed', inset: 0, zIndex: 10000, background: 'var(--bg)', display: 'grid', placeItems: 'center' }}><div className="card card-pad">
    <p>{status.data?.phase === 'waiting' ? tr(`环境已就绪，等待 ${status.data.activeTasks ?? 0} 个任务完成…`, `Runtime ready; waiting for ${status.data.activeTasks ?? 0} active tasks…`) : tr('正在切换 Python 并重新连接…', 'Switching Python and reconnecting…')}</p>
    {status.data?.phase === 'waiting' && <button className="btn btn-soft" onClick={() => void pythonHost.cancel()}>{tr('取消切换，继续使用当前环境', 'Cancel switch and keep current runtime')}</button>}
  </div></div>;
}
