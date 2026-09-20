import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type ZoteroCollection } from '../../lib/api';
import { tr } from '../../lib/i18n';

interface CollectionOption {
  key: string;
  name: string;
  depth: number;
}

export function isBlockingZoteroBindingError(error: unknown): boolean {
  return error != null && !(error instanceof ApiError && error.status === 404);
}

function flattenCollections(collections: ZoteroCollection[]): CollectionOption[] {
  const byParent = new Map<string | null, ZoteroCollection[]>();
  for (const collection of collections) {
    const siblings = byParent.get(collection.parent_key) ?? [];
    siblings.push(collection);
    byParent.set(collection.parent_key, siblings);
  }
  const rows: CollectionOption[] = [];
  const visit = (parent: string | null, depth: number) => {
    for (const collection of byParent.get(parent) ?? []) {
      rows.push({ key: collection.key, name: collection.name, depth });
      visit(collection.key, depth + 1);
    }
  };
  visit(null, 0);
  return rows;
}

function syncStatusLabel(value: string | null | undefined): string {
  const labels: Record<string, [string, string]> = {
    idle: ['空闲', 'Idle'],
    ready: ['已连接', 'Ready'],
    queued: ['等待同步', 'Queued'],
    running: ['同步中', 'Running'],
    completed: ['已完成', 'Completed'],
    failed: ['失败', 'Failed'],
    error: ['异常', 'Error'],
    missing: ['已移除', 'Missing'],
  };
  const label = value ? labels[value] : undefined;
  return label ? tr(label[0], label[1]) : (value ?? tr('未知', 'Unknown'));
}

export function ZoteroLocalSyncModal({
  libraryId,
  open,
  onClose,
}: {
  libraryId: string;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [collectionKey, setCollectionKey] = useState('');
  const [confirmUnbind, setConfirmUnbind] = useState(false);

  const probe = useQuery({
    queryKey: ['zotero-local-probe'],
    queryFn: () => api.probeZoteroLocal(),
    enabled: open,
    retry: false,
  });
  const binding = useQuery({
    queryKey: ['zotero-local-binding', libraryId],
    queryFn: () => api.getZoteroBinding(libraryId),
    enabled: open,
    retry: false,
  });
  const collections = useQuery({
    queryKey: ['zotero-local-collections'],
    queryFn: () => api.listZoteroCollections(),
    enabled: open && probe.data?.available === true,
    retry: false,
  });
  const status = useQuery({
    queryKey: ['zotero-local-status', libraryId],
    queryFn: () => api.getZoteroSyncStatus(libraryId),
    enabled: open && !!binding.data,
    retry: false,
    refetchInterval: (query) => {
      const state = query.state.data?.status;
      // Keep a light idle poll while the modal is open so scheduled/background runs also show up.
      return state === 'queued' || state === 'running' ? 1_500 : 10_000;
    },
  });

  const options = useMemo(() => flattenCollections(collections.data ?? []), [collections.data]);
  useEffect(() => {
    if (binding.data?.collection_key) setCollectionKey(binding.data.collection_key);
  }, [binding.data?.collection_key]);
  useEffect(() => {
    if (!collectionKey && options[0]) setCollectionKey(options[0].key);
  }, [collectionKey, options]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['zotero-local-binding', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['zotero-local-status', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['papers', libraryId] });
  };

  const bindMutation = useMutation({
    mutationFn: () => {
      const selected = options.find((option) => option.key === collectionKey);
      if (!selected) throw new Error('COLLECTION_REQUIRED');
      return api.putZoteroBinding(libraryId, {
        collection_key: selected.key,
      });
    },
    onSuccess: async () => {
      toast(tr('Zotero Collection 已绑定', 'Zotero collection connected'), 'ok');
      refresh();
      try {
        await api.startZoteroSync(libraryId);
        refresh();
      } catch (error) {
        toast(`${tr('绑定成功，但首次同步启动失败', 'Connected, but initial sync could not start')}：${error instanceof Error ? error.message : String(error)}`, 'error');
      }
    },
    onError: (error) => toast(`${tr('绑定失败', 'Connection failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const syncMutation = useMutation({
    mutationFn: (full: boolean) => api.startZoteroSync(libraryId, full),
    onSuccess: () => {
      toast(tr('Zotero 同步已排队', 'Zotero sync queued'), 'ok');
      refresh();
    },
    onError: (error) => toast(`${tr('无法启动同步', 'Could not start sync')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const unbindMutation = useMutation({
    mutationFn: () => api.deleteZoteroBinding(libraryId),
    onSuccess: () => {
      setCollectionKey('');
      setConfirmUnbind(false);
      queryClient.removeQueries({ queryKey: ['zotero-local-binding', libraryId], exact: true });
      queryClient.removeQueries({ queryKey: ['zotero-local-status', libraryId], exact: true });
      void queryClient.invalidateQueries({ queryKey: ['papers', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['paper'] });
      toast(tr('已解除 Zotero 绑定，现有论文不会删除', 'Zotero disconnected; existing papers were kept'), 'ok');
    },
    onError: (error) => toast(`${tr('解除绑定失败', 'Disconnect failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  const run = status.data;
  const active = run?.status === 'queued' || run?.status === 'running';
  const serverOnly = probe.error instanceof ApiError && probe.error.status === 409;
  const bindingFailed = binding.isError && isBlockingZoteroBindingError(binding.error);
  const statusUnavailable = !!binding.data && (status.isLoading || status.isError);

  useEffect(() => {
    if (!run || active) return;
    // The first invalidation happens before the worker writes. Refresh again at the durable
    // terminal state so newly imported/archived papers and binding timestamps become visible.
    void queryClient.invalidateQueries({ queryKey: ['papers', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
    void queryClient.invalidateQueries({ queryKey: ['zotero-local-binding', libraryId] });
  }, [active, libraryId, queryClient, run?.finished_at, run?.id, run?.status]);

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        width={620}
        title={tr('连接本机 Zotero', 'Connect local Zotero')}
        sub={tr('只读同步所选 Collection 及全部子 Collection；PDF 在阅读或生成总结时才复制。', 'Read-only sync for the selected collection and all descendants. PDFs are copied only when needed.')}
        footer={<button className="btn btn-ghost sm" onClick={onClose}>{tr('关闭', 'Close')}</button>}
      >
      {probe.isLoading ? (
        <div className="empty">{tr('正在检测 Zotero…', 'Probing Zotero…')}</div>
      ) : probe.data?.available ? (
        <>
          <div className="row gap8 wrap" style={{ marginBottom: 14 }}>
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <Icon name="check" size={11} />
              {tr('Zotero 已连接', 'Zotero connected')}
            </span>
            {probe.data.zotero_version && <span className="mono muted" style={{ fontSize: 11 }}>v{probe.data.zotero_version}</span>}
            {probe.data.api_version != null && <span className="mono muted" style={{ fontSize: 11 }}>API {probe.data.api_version}</span>}
          </div>

          <label className="col gap6" style={{ fontSize: 12 }}>
            <span>{tr('同步的 Collection', 'Collection to sync')}</span>
            <select
              className="input"
              value={collectionKey}
              onChange={(event) => setCollectionKey(event.target.value)}
              disabled={collections.isLoading || binding.isLoading || bindingFailed || !!binding.data || options.length === 0}
            >
              {!collectionKey && (
                <option value="" disabled>
                  {collections.isLoading
                    ? tr('正在读取 Collection…', 'Loading collections…')
                    : tr('没有可用 Collection', 'No collections available')}
                </option>
              )}
              {options.map((option) => (
                <option key={option.key} value={option.key}>
                  {'　'.repeat(option.depth)}{option.name}
                </option>
              ))}
            </select>
          </label>

          {collections.isLoading && (
            <div className="muted" style={{ marginTop: 7, fontSize: 11.5 }}>
              {tr('正在读取 Zotero Collection 树…', 'Loading the Zotero collection tree…')}
            </div>
          )}
          {!collections.isLoading && !collections.isError && options.length === 0 && (
            <div className="muted" style={{ marginTop: 7, fontSize: 11.5 }}>
              {tr('Zotero 中还没有可绑定的 Collection。请先创建 Collection 后重新读取。', 'No Zotero collections are available yet. Create one in Zotero, then reload.')}
            </div>
          )}

          {binding.data ? (
            <div className="card card-pad" style={{ marginTop: 16 }}>
              <div className="row gap8 wrap" style={{ justifyContent: 'space-between' }}>
                <div>
                  <div style={{ fontWeight: 650 }}>{binding.data.collection_name}</div>
                  <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
                    {tr('递归包含子 Collection', 'Includes descendant collections')}
                    {binding.data.last_synced_at ? ` · ${tr('上次', 'Last')} ${new Date(binding.data.last_synced_at).toLocaleString()}` : ''}
                  </div>
                </div>
                <span className="pill sm">{syncStatusLabel(binding.data.status)}</span>
              </div>
              {run && (
                <div style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.7 }}>
                  <div>{tr('运行状态', 'Run status')}：<span>{syncStatusLabel(run.status)}</span></div>
                  <div>
                    {tr('扫描/新增/更新/归档/失败', 'Scanned/created/updated/archived/failed')}：
                    <span className="mono">{run.processed}/{run.created}/{run.updated}/{run.missing}/{run.failed}</span>
                  </div>
                  {run.error_samples?.slice(0, 3).map((sample, index) => (
                    <div key={`${run.id}-error-${index}`} style={{ color: 'var(--danger-tx)' }}>
                      {String(sample.item_key ?? tr('同步', 'sync'))}：{String(sample.error ?? tr('未知错误', 'unknown error'))}
                    </div>
                  ))}
                </div>
              )}
              {binding.data.last_error && (
                <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--danger-tx)' }}>
                  {binding.data.last_error}
                </div>
              )}
              {binding.data.next_sync_at && (
                <div className="muted" style={{ marginTop: 6, fontSize: 11 }}>
                  {tr('下次检查', 'Next check')}：{new Date(binding.data.next_sync_at).toLocaleString()}
                </div>
              )}
              {status.isError && (
                <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--danger-tx)' }} role="alert">
                  {tr('无法确认当前同步状态；为避免重复任务，已暂停同步操作。', 'Could not verify the current sync state. Sync actions are paused to avoid duplicate jobs.')}
                  <button className="btn btn-ghost sm" style={{ marginLeft: 8 }} onClick={() => void status.refetch()}>
                    {tr('重试', 'Retry')}
                  </button>
                </div>
              )}
              <div className="row gap8 wrap" style={{ marginTop: 14 }}>
                <button className="btn btn-primary sm" disabled={active || statusUnavailable || syncMutation.isPending} onClick={() => syncMutation.mutate(false)}>
                  <Icon name="refresh" size={12} style={active ? { animation: 'spin 1s linear infinite' } : undefined} />
                  {active ? tr('同步中…', 'Syncing…') : tr('立即同步', 'Sync now')}
                </button>
                <button className="btn btn-soft sm" disabled={active || statusUnavailable || syncMutation.isPending} onClick={() => syncMutation.mutate(true)}>
                  {tr('重新全量核对', 'Full reconciliation')}
                </button>
                <button className="btn btn-ghost sm" style={{ marginLeft: 'auto', color: 'var(--danger-tx)' }} disabled={active || unbindMutation.isPending} onClick={() => setConfirmUnbind(true)}>
                  {tr('解除绑定', 'Disconnect')}
                </button>
              </div>
            </div>
          ) : binding.isLoading ? (
            <div className="muted" style={{ marginTop: 14, fontSize: 11.5 }}>
              {tr('正在读取当前绑定…', 'Loading the current connection…')}
            </div>
          ) : bindingFailed ? (
            <div style={{ marginTop: 14, fontSize: 11.5, color: 'var(--danger-tx)' }} role="alert">
              {tr('无法读取当前 Zotero 绑定。为避免覆盖现有配置，绑定操作已暂停。', 'Could not load the current Zotero connection. Connecting is paused to avoid overwriting an existing binding.')}
              <button className="btn btn-ghost sm" style={{ marginLeft: 8 }} onClick={() => void binding.refetch()}>
                {tr('重试', 'Retry')}
              </button>
            </div>
          ) : (
            <button className="btn btn-primary sm" style={{ marginTop: 14 }} disabled={!collectionKey || bindMutation.isPending} onClick={() => bindMutation.mutate()}>
              <Icon name="link" size={12} />
              {bindMutation.isPending ? tr('绑定中…', 'Connecting…') : tr('绑定并首次同步', 'Connect and sync')}
            </button>
          )}
          {collections.error && (
            <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--danger-tx)' }}>
              {tr('无法读取 Collection', 'Could not load collections')}：
              {collections.error instanceof Error ? collections.error.message : String(collections.error)}
            </div>
          )}
        </>
      ) : (
        <div className="card card-pad" style={{ color: 'var(--text-2)', lineHeight: 1.65 }}>
          <div style={{ fontWeight: 650, marginBottom: 5 }}>
            {serverOnly ? tr('仅 Desktop 支持本机 Zotero', 'Local Zotero requires Desktop') : tr('未检测到 Zotero Local API', 'Zotero Local API was not found')}
          </div>
          <div style={{ fontSize: 12 }}>
            {serverOnly
              ? tr('Server 继续使用现有的 .bib + ZIP 导入。', 'Server keeps the existing .bib + ZIP import flow.')
              : tr('请启动 Zotero，并在 Zotero 设置中启用本地 API 后重试。', 'Start Zotero and enable its local API, then retry.')}
          </div>
          <button className="btn btn-soft sm" style={{ marginTop: 12 }} onClick={() => void probe.refetch()}>
            <Icon name="refresh" size={12} />{tr('重新检测', 'Probe again')}
          </button>
        </div>
      )}
      </Modal>
      <ConfirmModal
        open={confirmUnbind}
        onClose={() => setConfirmUnbind(false)}
        title={tr('解除 Zotero 绑定', 'Disconnect Zotero')}
        message={tr(
          '解除后将停止 Collection 增量同步；已经同步到 Polaris 的论文、PDF 和总结都会保留。',
          'This stops incremental collection sync. Papers, PDFs, and summaries already in Polaris are kept.',
        )}
        confirmText={tr('解除绑定', 'Disconnect')}
        danger
        busy={unbindMutation.isPending}
        onConfirm={() => unbindMutation.mutate()}
      />
    </>
  );
}
