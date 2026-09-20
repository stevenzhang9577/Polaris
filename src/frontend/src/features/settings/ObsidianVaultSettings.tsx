import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { Modal } from '../../components/ui/Modal';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type VaultConflict } from '../../lib/api';
import {
  CAPABILITY_OBSIDIAN_VAULT_SYNC,
  isCapabilityAvailable,
  loadCapabilities,
  pickObsidianVaultDirectory,
} from '../../lib/host';
import { tr } from '../../lib/i18n';
import { localOrigin } from '../../lib/endpoint';

export function ObsidianVaultSettings() {
  const queryClient = useQueryClient();
  const [available, setAvailable] = useState(
    () => localOrigin() !== null && isCapabilityAvailable(CAPABILITY_OBSIDIAN_VAULT_SYNC),
  );
  const [selectedConflict, setSelectedConflict] = useState<VaultConflict | null>(null);
  const [mergedContent, setMergedContent] = useState('');
  const [pendingConnectionAction, setPendingConnectionAction] = useState<'change' | 'disconnect' | null>(null);
  const [pendingConnectionId, setPendingConnectionId] = useState<string | null>(null);
  const [directoryDraft, setDirectoryDraft] = useState<string | null>(null);
  const [confirmDirectory, setConfirmDirectory] = useState(false);

  useEffect(() => {
    if (available) return;
    let alive = true;
    void loadCapabilities().then(() => {
      if (alive) {
        setAvailable(
          localOrigin() !== null && isCapabilityAvailable(CAPABILITY_OBSIDIAN_VAULT_SYNC),
        );
      }
    });
    return () => { alive = false; };
  }, [available]);

  const status = useQuery({
    queryKey: ['obsidian-vault'],
    queryFn: () => api.getObsidianVault(),
    enabled: available,
    retry: false,
    refetchInterval: (query) => query.state.data?.connection ? 5_000 : false,
  });
  const libraries = useQuery({
    queryKey: ['libraries', 'obsidian-settings'],
    queryFn: () => api.listLibraries({ type: 'all' }),
    enabled: available && !!status.data?.connection,
    retry: false,
  });
  const conflicts = useQuery({
    queryKey: ['obsidian-vault-conflicts'],
    queryFn: () => api.listObsidianConflicts('open'),
    enabled: available && !!status.data?.connection,
    retry: false,
    refetchInterval: 3_000,
  });

  const latestConflict = conflicts.data?.find((item) => item.id === selectedConflict?.id);
  const currentDirectory = status.data?.connection?.managed_directory ?? 'Polaris';
  const managedDirectory = directoryDraft ?? currentDirectory;
  const validDirectory = managedDirectory.length > 0 && managedDirectory.length <= 128
    && managedDirectory === managedDirectory.trim() && !managedDirectory.startsWith('.')
    && !managedDirectory.endsWith('.') && !/[/\\:<>"|?*\u0000-\u001f\u007f]/.test(managedDirectory);
  const directoryChanged = managedDirectory !== currentDirectory;
  const conflictChanged = !!selectedConflict && !!latestConflict
    && latestConflict.version !== selectedConflict.version;
  const conflictGone = !!selectedConflict && !!conflicts.data && !latestConflict;

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['obsidian-vault'] });
    void queryClient.invalidateQueries({ queryKey: ['obsidian-vault-conflicts'] });
  };

  const connect = useMutation({
    mutationFn: async (expectedConnectionId: string | null) => {
      const path = await pickObsidianVaultDirectory();
      if (!path) return null;
      const latest = await api.getObsidianVault();
      if ((latest.connection?.id ?? null) !== expectedConnectionId) {
        throw new Error('VAULT_CONNECTION_CHANGED');
      }
      return api.putObsidianVault(path, managedDirectory);
    },
    onSuccess: (result) => {
      if (!result) return;
      setPendingConnectionAction(null);
      setPendingConnectionId(null);
      setDirectoryDraft(null);
      refresh();
      toast(tr('Obsidian Vault 已连接', 'Obsidian Vault connected'), 'ok');
    },
    onError: (error) => {
      const detail = error instanceof Error && error.message === 'VAULT_CONNECTION_CHANGED'
        ? tr('Vault 连接已在其他窗口变化，请刷新状态后重试', 'The Vault connection changed in another window. Refresh and try again.')
        : error instanceof Error ? error.message : String(error);
      toast(`${tr('连接失败', 'Connection failed')}：${detail}`, 'error');
      setPendingConnectionAction(null);
      setPendingConnectionId(null);
      refresh();
    },
  });
  const changeDirectory = useMutation({
    mutationFn: async () => {
      const connection = status.data?.connection;
      if (!connection) throw new Error('VAULT_CONNECTION_CHANGED');
      return api.putObsidianVault(connection.vault_path, managedDirectory);
    },
    onSuccess: () => {
      setConfirmDirectory(false);
      setDirectoryDraft(null);
      refresh();
      toast(tr('同步目录已更新，文件与冲突记录已保留', 'Sync folder updated; files and conflicts were preserved'), 'ok');
    },
    onError: (error) => {
      const detail = error instanceof Error ? error.message : String(error);
      toast(detail.includes('OBSIDIAN_DESTINATION_ALREADY_EXISTS')
        ? tr('目标目录已存在，请使用尚未创建的目录名，避免覆盖已有文件。', 'The destination already exists. Choose a new folder name to avoid overwriting files.')
        : `${tr('更换目录失败，原配置保留', 'Folder change failed; the previous configuration was kept')}：${detail}`, 'error');
      setConfirmDirectory(false);
      refresh();
    },
  });
  const disconnect = useMutation({
    mutationFn: async (expectedConnectionId: string) => {
      const latest = await api.getObsidianVault();
      if (latest.connection?.id !== expectedConnectionId) {
        throw new Error('VAULT_CONNECTION_CHANGED');
      }
      return api.deleteObsidianVault();
    },
    onSuccess: () => {
      setPendingConnectionAction(null);
      setPendingConnectionId(null);
      refresh();
      toast(tr('已断开 Vault；磁盘里的文件保留不动', 'Vault disconnected; files on disk were kept'), 'ok');
    },
    onError: (error) => {
      const detail = error instanceof Error && error.message === 'VAULT_CONNECTION_CHANGED'
        ? tr('Vault 连接已在其他窗口变化，请刷新状态后重试', 'The Vault connection changed in another window. Refresh and try again.')
        : error instanceof Error ? error.message : String(error);
      toast(`${tr('断开失败', 'Disconnect failed')}：${detail}`, 'error');
      setPendingConnectionAction(null);
      setPendingConnectionId(null);
      refresh();
    },
  });
  const toggleLibrary = useMutation({
    mutationFn: ({ libraryId, enabled }: { libraryId: string; enabled: boolean }) => api.setObsidianLibrary(libraryId, enabled),
    onSuccess: (binding) => {
      refresh();
      if (binding.enabled) {
        toast(tr('已启用，首次投影正在后台进行', 'Enabled; the initial projection is running in the background'), 'ok');
      }
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const sync = useMutation({
    mutationFn: () => api.syncObsidianVault(),
    onSuccess: (result) => {
      refresh();
      toast(
        tr(
          `核对完成：写入 ${result.files_written}，导入 ${result.files_imported}，冲突 ${result.conflicts}`,
          `Reconciled: wrote ${result.files_written}, imported ${result.files_imported}, conflicts ${result.conflicts}`,
        ),
        result.errors.length ? 'error' : result.conflicts ? 'info' : 'ok',
      );
      if (result.errors.length) {
        toast(`${tr('部分文件同步失败', 'Some files failed to sync')}：${result.errors.slice(0, 3).join(' · ')}`, 'error');
      }
    },
    onError: (error) => toast(`${tr('同步失败', 'Sync failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const resolve = useMutation({
    mutationFn: ({ conflict, strategy, content }: { conflict: VaultConflict; strategy: 'polaris' | 'vault' | 'merged'; content?: string }) =>
      api.resolveObsidianConflict(conflict.id, { strategy, content, expected_version: conflict.version }),
    onSuccess: () => {
      setSelectedConflict(null);
      setMergedContent('');
      refresh();
      toast(tr('冲突已解决', 'Conflict resolved'), 'ok');
    },
    onError: async (error, variables) => {
      if (error instanceof ApiError && error.status === 409) {
        const latest = await conflicts.refetch();
        const updated = latest.data?.find((item) => item.id === variables.conflict.id);
        if (updated) setSelectedConflict(updated);
        // Keep mergedContent: a stale snapshot must never erase the user's merge draft.
        toast(tr('双方内容已变化，请核对刷新后的版本再提交；合并草稿已保留。', 'Content changed. Review the refreshed versions before submitting; your merge draft was kept.'), 'info');
        refresh();
        return;
      }
      toast(`${tr('解决失败', 'Resolution failed')}：${error instanceof Error ? error.message : String(error)}`, 'error');
    },
  });

  const enabledLibraries = useMemo(
    () => new Set((status.data?.bindings ?? []).filter((binding) => binding.enabled).map((binding) => binding.library_id)),
    [status.data?.bindings],
  );

  if (!available) {
    return (
      <div className="card card-pad">
        <div className="section-h">Obsidian Vault</div>
        <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
          {tr('这个能力只在 Polaris Desktop 中可用；Server 仍可使用只读 ZIP 导出。', 'This capability is available in Polaris Desktop only. Server can still use the read-only ZIP export.')}
        </p>
      </div>
    );
  }

  if (status.isLoading) {
    return (
      <div className="card card-pad">
        <div className="section-h">Obsidian Vault</div>
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          {tr('正在读取 Vault 连接状态…', 'Loading Vault connection status…')}
        </div>
      </div>
    );
  }

  if (status.isError) {
    return (
      <div className="card card-pad">
        <div className="section-h">Obsidian Vault</div>
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--danger-tx)' }}>
          {tr('无法读取 Vault 连接状态，已禁止更换或断开操作。', 'Could not load the Vault connection status. Change and disconnect actions are disabled.')}
        </div>
        <button className="btn btn-soft sm" style={{ marginTop: 12 }} onClick={() => void status.refetch()}>
          <Icon name="refresh" size={12} />{tr('重试', 'Retry')}
        </button>
      </div>
    );
  }

  return (
    <div className="col gap16">
      <div className="card card-pad">
        <div className="row gap8 wrap" style={{ justifyContent: 'space-between' }}>
          <div>
            <div className="section-h">Obsidian Vault</div>
            <div className="muted" style={{ fontSize: 12, marginTop: 5, lineHeight: 1.55 }}>
              {tr('只管理你指定的 Vault 子目录；不会复制 PDF，也不会扫描 Vault 的其他目录。', 'Only your chosen Vault subfolder is managed. PDFs are not copied and other Vault folders are not scanned.')}
            </div>
          </div>
          {status.data?.connection ? (
            <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
              <Icon name="check" size={11} />{status.data.connection.watching ? tr('监听中', 'Watching') : tr('已连接', 'Connected')}
            </span>
          ) : null}
        </div>

        <div className="row gap8 wrap" style={{ marginTop: 16 }}>
          <button
            className="btn btn-primary sm"
            disabled={connect.isPending || changeDirectory.isPending || !validDirectory}
            onClick={() => {
              const connection = status.data?.connection;
              if (!connection) {
                connect.mutate(null);
                return;
              }
              setPendingConnectionId(connection.id);
              setPendingConnectionAction('change');
            }}
          >
            <Icon name="file" size={12} />
            {status.data?.connection ? tr('更换 Vault', 'Change Vault') : tr('选择 Vault', 'Choose Vault')}
          </button>
          {status.data?.connection && (
            <>
              <button className="btn btn-soft sm" disabled={sync.isPending} onClick={() => sync.mutate()}>
                <Icon name="refresh" size={12} style={sync.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
                {sync.isPending ? tr('核对中…', 'Reconciling…') : tr('立即核对', 'Reconcile now')}
              </button>
              <button
                className="btn btn-ghost sm"
                style={{ color: 'var(--danger-tx)' }}
                disabled={disconnect.isPending}
                onClick={() => {
                  const connection = status.data?.connection;
                  if (!connection) return;
                  setPendingConnectionId(connection.id);
                  setPendingConnectionAction('disconnect');
                }}
              >
                {tr('断开', 'Disconnect')}
              </button>
            </>
          )}
        </div>
        {status.data?.connection?.vault_path && (
          <div className="mono muted" style={{ marginTop: 10, fontSize: 11, overflowWrap: 'anywhere' }}>
            {status.data.connection.vault_path}
          </div>
        )}
        <div style={{ marginTop: 18, borderTop: '1px solid var(--border)', paddingTop: 16 }}>
          <label className="col gap6" htmlFor="obsidian-managed-directory" style={{ fontSize: 12.5, fontWeight: 600 }}>
            {tr('Vault 内的同步目录', 'Sync folder inside the Vault')}
          </label>
          <div className="row gap8 wrap" style={{ marginTop: 8 }}>
            <input
              id="obsidian-managed-directory"
              className="input"
              style={{ flex: '1 1 220px', minWidth: 0, maxWidth: 420 }}
              value={managedDirectory}
              maxLength={128}
              placeholder="008-Polaris"
              disabled={connect.isPending || changeDirectory.isPending}
              onChange={(event) => setDirectoryDraft(event.target.value)}
              aria-invalid={!validDirectory}
              aria-describedby="obsidian-directory-help"
            />
            {status.data?.connection && (
              <button className="btn btn-primary sm" disabled={!validDirectory || !directoryChanged || changeDirectory.isPending || connect.isPending} onClick={() => setConfirmDirectory(true)}>
                {changeDirectory.isPending ? tr('正在更换…', 'Changing…') : tr('保存目录', 'Save folder')}
              </button>
            )}
          </div>
          <div id="obsidian-directory-help" className="muted" style={{ marginTop: 8, fontSize: 11.5, lineHeight: 1.6 }}>
            {tr('填写一个目录名，例如 008-Polaris，不是完整路径。更换时会重命名当前同步目录，保留未同步编辑、历史基线与冲突；目标目录须尚不存在。', 'Enter a folder name, such as 008-Polaris, not a full path. Changing it renames the current sync folder while preserving unsynced edits, baselines and conflicts. The destination must not already exist.')}
          </div>
          <div className="mono muted" style={{ marginTop: 6, fontSize: 11, overflowWrap: 'anywhere' }}>
            {status.data?.connection?.vault_path ?? '<Vault>'}/{managedDirectory}/
          </div>
          {!validDirectory && <div role="alert" style={{ color: 'var(--danger-tx)', fontSize: 11.5, marginTop: 6 }}>{tr('请输入有效目录名：不能以点开头、包含斜杠或特殊路径字符。', 'Enter a valid folder name without a leading dot, slashes or special path characters.')}</div>}
        </div>
        {status.data?.connection?.last_synced_at && (
          <div className="muted" style={{ marginTop: 10, fontSize: 11.5 }}>
            {tr('上次同步', 'Last sync')}：{new Date(status.data.connection.last_synced_at).toLocaleString()}
          </div>
        )}
        {status.data?.connection?.last_error && (
          <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--danger-tx)' }}>{status.data.connection.last_error}</div>
        )}
      </div>

      {status.data?.connection && (
        <div className="card card-pad">
          <div className="section-h">{tr('同步的文献库', 'Synced libraries')}</div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 5 }}>
            {tr('总结是通用版本，个人笔记只同步当前用户自己的内容。', 'Summaries are shared; only your own private notes are synchronized.')}
          </div>
          <div style={{ marginTop: 12 }}>
            {libraries.isLoading ? (
              <div className="muted" style={{ fontSize: 12 }}>
                {tr('正在读取可同步的文献库…', 'Loading libraries available for sync…')}
              </div>
            ) : libraries.isError ? (
              <div style={{ fontSize: 12, color: 'var(--danger-tx)' }} role="alert">
                {tr('无法读取文献库；现有同步开关没有被修改。', 'Could not load libraries. Existing sync settings were not changed.')}
                <button className="btn btn-ghost sm" style={{ marginLeft: 8 }} onClick={() => void libraries.refetch()}>
                  {tr('重试', 'Retry')}
                </button>
              </div>
            ) : (libraries.data?.length ?? 0) === 0 ? (
              <div className="muted" style={{ fontSize: 12 }}>
                {tr('还没有可管理的文献库。创建文献库后即可在这里启用同步。', 'No manageable libraries are available yet. Create a library to enable sync here.')}
              </div>
            ) : (libraries.data ?? []).map((library) => {
              const enabled = enabledLibraries.has(library.id);
              return (
                <div key={library.id} className="row gap8" style={{ padding: '9px 0', borderBottom: '0.5px solid var(--border)' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 620 }}>{library.name}</div>
                    <div className="muted" style={{ fontSize: 11 }}>
                      {library.paper_count} {tr('篇论文', 'papers')}
                      {!library.can_manage ? ` · ${tr('仅可读', 'read only')}` : ''}
                    </div>
                  </div>
                  <Switch
                    checked={enabled}
                    disabled={toggleLibrary.isPending || !library.can_manage}
                    aria-label={tr(`同步 ${library.name}`, `Sync ${library.name}`)}
                    onChange={(checked) => toggleLibrary.mutate({ libraryId: library.id, enabled: checked })}
                  />
                </div>
              );
            })}
          </div>
        </div>
      )}

      {status.data?.connection && (
        <div className="card card-pad">
          <div className="row gap8" style={{ justifyContent: 'space-between' }}>
            <div className="section-h">{tr('同步冲突', 'Sync conflicts')}</div>
            <span className="pill sm">{conflicts.data?.length ?? status.data.conflict_count}</span>
          </div>
          {conflicts.isLoading ? (
            <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>{tr('正在检查冲突…', 'Checking conflicts…')}</div>
          ) : conflicts.error ? (
            <div style={{ marginTop: 10, fontSize: 12, color: 'var(--danger-tx)' }}>
              {tr('无法读取冲突', 'Could not load conflicts')}
              <button className="btn btn-ghost sm" style={{ marginLeft: 8 }} onClick={() => void conflicts.refetch()}>{tr('重试', 'Retry')}</button>
            </div>
          ) : (conflicts.data?.length ?? 0) === 0 ? (
            <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>{tr('没有待解决冲突。', 'No unresolved conflicts.')}</div>
          ) : (
            <div style={{ marginTop: 10 }}>
              {(conflicts.data ?? []).map((conflict) => (
                <div key={conflict.id} className="row gap8" style={{ padding: '9px 0', borderBottom: '0.5px solid var(--border)' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="mono" style={{ fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>{conflict.relative_path}</div>
                    <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{conflict.entity_type}</div>
                  </div>
                  <button className="btn btn-soft sm" onClick={() => {
                    setSelectedConflict(conflict);
                    setMergedContent(conflict.vault_content);
                  }}>{tr('解决', 'Resolve')}</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <Modal
        open={!!selectedConflict}
        onClose={() => {
          setSelectedConflict(null);
          setMergedContent('');
        }}
        title={tr('解决 Vault 冲突', 'Resolve Vault conflict')}
        width={760}
        footer={selectedConflict ? (
          <>
            <button className="btn btn-soft sm" disabled={resolve.isPending || conflictChanged || conflictGone} onClick={() => resolve.mutate({ conflict: selectedConflict, strategy: 'polaris' })}>{tr('采用 Polaris', 'Use Polaris')}</button>
            <button className="btn btn-soft sm" disabled={resolve.isPending || conflictChanged || conflictGone} onClick={() => resolve.mutate({ conflict: selectedConflict, strategy: 'vault' })}>{tr('采用 Vault', 'Use Vault')}</button>
            <button className="btn btn-primary sm" disabled={resolve.isPending || conflictChanged || conflictGone || !mergedContent.trim()} onClick={() => resolve.mutate({ conflict: selectedConflict, strategy: 'merged', content: mergedContent })}>{tr('保存合并结果', 'Save merged result')}</button>
          </>
        ) : undefined}
      >
        {selectedConflict && (
          <>
            {conflictChanged && latestConflict && (
              <div style={{ marginBottom: 12, color: 'var(--warn-tx)', fontSize: 12 }} role="status">
                {tr('检测到新编辑，请刷新双方版本后重新核对。合并草稿会保留。', 'New edits detected. Refresh both versions and review them; your merge draft is kept.')}
                <button className="btn btn-soft sm" style={{ marginTop: 8 }} onClick={() => setSelectedConflict(latestConflict)}>
                  {tr('刷新版本，保留草稿', 'Refresh versions, keep draft')}
                </button>
              </div>
            )}
            {conflictGone && (
              <div style={{ marginBottom: 12, color: 'var(--warn-tx)', fontSize: 12 }} role="status">
                {tr('此冲突已在其他窗口解决。你的草稿仍保留在下方，可复制后关闭。', 'This conflict was resolved elsewhere. Your draft remains below for copying before you close.')}
              </div>
            )}
            <div className="settings-2col" style={{ marginBottom: 12 }}>
              <div><div className="mono muted" style={{ fontSize: 10.5, marginBottom: 5 }}>Polaris</div><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 180, overflow: 'auto', fontSize: 11 }}>{selectedConflict.polaris_content}</pre></div>
              <div><div className="mono muted" style={{ fontSize: 10.5, marginBottom: 5 }}>Vault</div><pre style={{ whiteSpace: 'pre-wrap', maxHeight: 180, overflow: 'auto', fontSize: 11 }}>{selectedConflict.vault_content}</pre></div>
            </div>
            <label className="col gap6" style={{ fontSize: 12 }}>
              <span>{tr('编辑合并结果', 'Edit merged result')}</span>
              <textarea className="textarea mono" style={{ minHeight: 220, resize: 'vertical' }} value={mergedContent} onChange={(event) => setMergedContent(event.target.value)} />
            </label>
          </>
        )}
      </Modal>
      <ConfirmModal
        open={confirmDirectory}
        onClose={() => { if (!changeDirectory.isPending) setConfirmDirectory(false); }}
        title={tr('更换同步目录', 'Change sync folder')}
        message={tr(
          `将 ${currentDirectory}/ 重命名为 ${managedDirectory}/。现有文件、未同步编辑与冲突记录会保留；PDF 不会复制。请暂时关闭正在编辑这些文件的窗口。`,
          `Rename ${currentDirectory}/ to ${managedDirectory}/. Existing files, unsynced edits and conflicts will be preserved; PDFs are not copied. Close editors currently editing these files before continuing.`,
        )}
        confirmText={tr('确认更换', 'Change folder')}
        busy={changeDirectory.isPending}
        onConfirm={() => changeDirectory.mutate()}
      />
      <ConfirmModal
        open={pendingConnectionAction === 'change'}
        onClose={() => {
          setPendingConnectionAction(null);
          setPendingConnectionId(null);
        }}
        title={tr('更换 Obsidian Vault', 'Change Obsidian Vault')}
        message={tr(
          '现有同步文件会复制到新 Vault，未同步编辑、基线与冲突记录保留；旧 Vault 文件保留，但不再监听。新 Vault 的目标同步目录须尚不存在。',
          'Managed files are copied to the new Vault, preserving unsynced edits, baselines and conflicts. Old Vault files remain but are no longer watched. The destination folder in the new Vault must not already exist.',
        )}
        confirmText={tr('选择新 Vault', 'Choose new Vault')}
        busy={connect.isPending}
        onConfirm={() => connect.mutate(pendingConnectionId)}
      />
      <ConfirmModal
        open={pendingConnectionAction === 'disconnect'}
        onClose={() => {
          setPendingConnectionAction(null);
          setPendingConnectionId(null);
        }}
        title={tr('断开 Obsidian Vault', 'Disconnect Obsidian Vault')}
        message={tr(
          '这会删除 Polaris 保存的同步基线和冲突记录，不会删除 Vault 中的文件。',
          'This removes Polaris sync baselines and conflict records. Files already written to the Vault are not deleted.',
        )}
        confirmText={tr('断开', 'Disconnect')}
        danger
        busy={disconnect.isPending}
        onConfirm={() => {
          if (pendingConnectionId) disconnect.mutate(pendingConnectionId);
        }}
      />
    </div>
  );
}
