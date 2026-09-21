import { ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { isTransientReadError } from '../../lib/read-query';

export function ReadFailure({ error, retry, fetching, retained = false }: {
  error: unknown;
  retry: () => unknown;
  fetching: boolean;
  retained?: boolean;
}) {
  const message = retained
    ? tr('刷新暂未成功，正在显示上次加载的内容。', 'Refresh failed; showing the last loaded content.')
    : error instanceof ApiError && error.status === 404
      ? tr('该资源不存在，或你没有访问权限。', 'This resource does not exist or is not accessible.')
      : isTransientReadError(error)
        ? tr('请求暂时失败，将自动重试。也可以立即重试。', 'The request failed temporarily and will retry automatically. You can also retry now.')
        : tr('请求失败，请重试。', 'The request failed. Please retry.');
  return <div role="status" style={{ padding: 12, color: 'var(--text-2)' }}>
    <div>{message}</div>
    <button className="btn" style={{ marginTop: 8 }} disabled={fetching} onClick={() => { void retry(); }}>
      {fetching ? tr('重试中…', 'Retrying…') : tr('重试', 'Retry')}
    </button>
  </div>;
}
