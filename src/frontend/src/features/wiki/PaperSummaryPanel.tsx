import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type PaperSummaryRevision } from '../../lib/api';
import { latestSummaryFailure, summaryErrorLabel, summaryModelLabel, summaryUtcTime } from './summaryStatus';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { localOrigin } from '../../lib/endpoint';

const STAGES = ['materialize', 'parse', 'compile', 'project'] as const;

function stageLabel(stage: typeof STAGES[number]): string {
  const labels: Record<typeof STAGES[number], [string, string]> = {
    materialize: ['获取全文', 'Get full text'],
    parse: ['解析论文', 'Parse paper'],
    compile: ['生成解读', 'Generate summary'],
    project: ['同步工作区', 'Sync workspace'],
  };
  return tr(labels[stage][0], labels[stage][1]);
}

function revisionStatusLabel(status: string): string {
  const labels: Record<string, [string, string]> = {
    queued: ['等待中', 'Queued'],
    generating: ['生成中', 'Generating'],
    ready: ['可用', 'Ready'],
    stale: ['需更新', 'Stale'],
    failed: ['失败', 'Failed'],
  };
  const label = labels[status];
  return label ? tr(label[0], label[1]) : status;
}

export function isSummaryRevisionInFlight(revision: PaperSummaryRevision): boolean {
  return revision.status === 'queued'
    || revision.status === 'generating'
    || (revision.status === 'ready' && revision.stage !== 'complete');
}

function sourceLabel(revision: PaperSummaryRevision): string {
  if (revision.status === 'queued' || (revision.status === 'generating' && ['materialize', 'parse'].includes(revision.stage ?? 'materialize'))) return tr('来源待确认', 'Source pending');
  if (revision.source_level === 'fulltext') return tr('全文级', 'Full text');
  if (revision.source_level === 'abstract') return tr('摘要级', 'Abstract only');
  if (revision.source_level === 'obsidian') {
    return revision.content_version_id
      ? tr('Obsidian · 全文源', 'Obsidian · full-text source')
      : tr('Obsidian · 摘要源', 'Obsidian · abstract source');
  }
  return tr('旧版', 'Legacy');
}

export function PaperSummaryPanel({ paperId, libraryId, canManage }: { paperId: string; libraryId?: string | null; canManage: boolean }) {
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [compareRevisionId, setCompareRevisionId] = useState<string | null>(null);
  const [activateRevisionId, setActivateRevisionId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const desktopLocal = localOrigin() !== null;

  const current = useQuery({
    queryKey: ['paper-summary', paperId],
    queryFn: () => api.getPaperSummary(paperId),
    retry: false,
    refetchInterval: desktopLocal ? 5_000 : false,
  });
  const history = useQuery({
    queryKey: ['paper-summary-history', paperId],
    queryFn: () => api.listPaperSummaries(paperId),
    retry: false,
    refetchInterval: (query) => {
      const inFlight = query.state.data?.some(isSummaryRevisionInFlight);
      // A local Vault edit can create a revision without a browser mutation. Keep a light poll
      // only in Desktop so the open paper reflects those external edits and conflicts quickly.
      return inFlight ? 1_500 : desktopLocal ? 5_000 : false;
    },
  });

  const running = useMemo(
    () => history.data?.find(isSummaryRevisionInFlight),
    [history.data],
  );
  const currentMissing = current.error instanceof ApiError && current.error.status === 404;
  const currentRevision = currentMissing ? undefined : current.data?.current_revision;
  const activeRevision = currentRevision ?? history.data?.find((revision) => revision.is_current);
  const compareRevision = history.data?.find((revision) => revision.id === compareRevisionId);
  const readyHistory = history.data?.filter((revision) => revision.status === 'ready' || revision.status === 'stale') ?? [];
  const softDeleted = current.error instanceof ApiError && current.error.status === 404 && readyHistory.some((revision) => revision.is_current);
  const latestFailure = latestSummaryFailure(history.data ?? []);
  const initialLoading = current.isLoading || history.isLoading;
  const loadFailed = history.isError || (current.isError && !currentMissing);

  const completedRevisionId = history.data?.find((revision) => revision.status === 'ready' && revision.stage === 'complete')?.id;
  const currentState = `${current.data?.current_revision.id ?? ''}:${current.data?.stale ?? ''}:${current.error instanceof ApiError ? current.error.status : ''}`;
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
    void queryClient.invalidateQueries({ queryKey: ['papers'] });
    void queryClient.invalidateQueries({ queryKey: ['paper-summary-history', paperId] });
  }, [currentState, paperId, queryClient]);
  useEffect(() => {
    if (!completedRevisionId) return;
    void queryClient.invalidateQueries({ queryKey: ['paper-summary', paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
    void queryClient.invalidateQueries({ queryKey: ['papers'] });
  }, [completedRevisionId, paperId, queryClient]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['paper-summary', paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper-summary-history', paperId] });
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
    void queryClient.invalidateQueries({ queryKey: ['papers'] });
  };

  const generate = useMutation({
    mutationFn: () => api.generatePaperSummary(paperId, libraryId),
    onSuccess: () => {
      setActivateRevisionId(null);
      refresh();
      toast(tr('总结任务已排队', 'Summary generation queued'), 'ok');
    },
    onError: (error) => toast(`${tr('无法生成总结', 'Could not generate summary')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const activate = useMutation({
    mutationFn: (revisionId: string) => api.activatePaperSummary(paperId, revisionId),
    onSuccess: () => {
      setConfirmDelete(false);
      refresh();
      toast(tr('已切换当前版本', 'Current revision changed'), 'ok');
    },
    onError: (error) => toast(`${tr('切换失败', 'Activation failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const remove = useMutation({
    mutationFn: () => api.deletePaperSummary(paperId),
    onSuccess: () => {
      refresh();
      toast(tr('总结已移入 30 天回收站', 'Summary moved to the 30-day trash'), 'ok');
    },
    onError: (error) => toast(`${tr('删除失败', 'Delete failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const restore = useMutation({
    mutationFn: () => api.restorePaperSummary(paperId),
    onSuccess: () => {
      refresh();
      toast(tr('总结已恢复', 'Summary restored'), 'ok');
    },
    onError: (error) => toast(`${tr('恢复失败', 'Restore failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  if (initialLoading) {
    return (
      <div className="card card-pad" style={{ marginTop: 16 }} aria-live="polite">
        <div className="section-h">{tr('论文总结', 'Paper summary')}</div>
        <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          {tr('正在读取总结与版本历史…', 'Loading the summary and revision history…')}
        </div>
      </div>
    );
  }

  if (loadFailed) {
    return (
      <div className="card card-pad" style={{ marginTop: 16 }}>
        <div className="section-h">{tr('论文总结', 'Paper summary')}</div>
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--danger-tx)' }}>
          {tr('无法读取总结状态，已暂停所有写操作。', 'Could not load summary state. All write actions are paused.')}
          <button className="btn btn-ghost sm" style={{ marginLeft: 8 }} onClick={() => {
            void current.refetch();
            void history.refetch();
          }}>
            {tr('重试', 'Retry')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card card-pad" style={{ marginTop: 16 }}>
      <div className="row gap8 wrap" style={{ justifyContent: 'space-between' }}>
        <div className="row gap8 wrap">
          <span className="section-h" style={{ margin: 0 }}>{tr('论文总结', 'Paper summary')}</span>
          {currentRevision && (
            <span className="pill sm" style={{
              background: currentRevision.source_level === 'abstract' || (currentRevision.source_level === 'obsidian' && !currentRevision.content_version_id) ? 'var(--warn-bg)' : 'var(--ok-bg)',
              color: currentRevision.source_level === 'abstract' || (currentRevision.source_level === 'obsidian' && !currentRevision.content_version_id) ? 'var(--warn-tx)' : 'var(--ok-tx)',
            }}>
              {sourceLabel(currentRevision)}
            </span>
          )}
          {!softDeleted && current.data?.stale && <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>{tr('需更新', 'Stale')}</span>}
          {softDeleted && <span className="pill sm">{tr('回收站', 'Trash')}</span>}
          {!canManage && <span className="pill sm">{tr('只读', 'Read only')}</span>}
        </div>
        <div className="row gap6 wrap">
          {canManage && (softDeleted ? (
            <button className="btn btn-soft sm" disabled={restore.isPending} onClick={() => restore.mutate()}>
              {tr('恢复', 'Restore')}
            </button>
          ) : (
            <button className="btn btn-primary sm" disabled={!!running || generate.isPending} onClick={() => generate.mutate()}>
              <Icon name={running ? 'refresh' : 'sparkle'} size={12} style={running ? { animation: 'spin 1s linear infinite' } : undefined} />
              {running ? tr('生成中…', 'Generating…') : currentRevision ? tr('重新生成', 'Regenerate') : tr('生成总结', 'Generate summary')}
            </button>
          ))}
          {(history.data?.length ?? 0) > 0 && (
            <button className="btn btn-soft sm" onClick={() => setHistoryOpen((value) => !value)}>
              <Icon name="clock" size={12} />
              {tr(`历史 ${history.data?.length ?? 0}`, `History ${history.data?.length ?? 0}`)}
            </button>
          )}
          {canManage && currentRevision && !softDeleted && (
            <button className="btn btn-ghost sm" style={{ color: 'var(--danger-tx)' }} disabled={!!running || remove.isPending} onClick={() => setConfirmDelete(true)}>
              {tr('删除总结', 'Delete summary')}
            </button>
          )}
        </div>
      </div>

      {currentRevision && (currentRevision.source_level === 'abstract' || (currentRevision.source_level === 'obsidian' && !currentRevision.content_version_id)) && (
        <div style={{ marginTop: 9, fontSize: 11.5, lineHeight: 1.55, color: 'var(--warn-tx)' }}>
          {tr('当前内容只依据标题与摘要生成；取得 PDF 全文后会标记为 stale，由你决定何时重新生成。', 'This revision uses title and abstract only. Once full text becomes available it is marked stale; regeneration remains under your control.')}
        </div>
      )}

      {running && (
        <div style={{ marginTop: 12 }} aria-live="polite">
          <div className="row gap6 wrap">
            {STAGES.map((stage) => {
              const currentIndex = STAGES.indexOf((running.stage ?? 'materialize') as typeof STAGES[number]);
              const index = STAGES.indexOf(stage);
              const done = currentIndex > index;
              const active = currentIndex === index;
              return (
                <span key={stage} className="pill sm" style={active ? { background: 'var(--accent-soft)', color: 'var(--accent-text)' } : done ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' } : undefined}>
                  {done ? '✓ ' : ''}{stageLabel(stage)}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {!running && latestFailure && (
        <div style={{ marginTop: 10, padding: '8px 10px', border: '1px solid var(--danger)', borderRadius: 8, color: 'var(--danger-tx)', fontSize: 11.5 }}>
          {tr('最近一次生成失败', 'The latest generation failed')}
          {latestFailure.error_code ? `：${summaryErrorLabel(latestFailure.error_code)}` : ''}
        </div>
      )}

      {historyOpen && (
        <div style={{ marginTop: 14, borderTop: '0.5px solid var(--border)', paddingTop: 10 }}>
          {(history.data ?? []).map((revision) => (
            <div key={revision.id} className="row gap8" style={{ padding: '8px 0', borderBottom: '0.5px solid var(--border)', alignItems: 'center' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="row gap6 wrap" style={{ fontSize: 11.5 }}>
                  <span className="mono">{fmtTime(summaryUtcTime(revision.created_at))}</span>
                  <span className="pill sm">{sourceLabel(revision)}</span>
                  <span className="pill sm">{revision.error_code === 'SUMMARY_CANCELLED' ? tr('已取消', 'Cancelled') : revisionStatusLabel(revision.status)}</span>
                  {revision.is_current && <span style={{ color: 'var(--accent-text)' }}>{tr('当前', 'Current')}</span>}
                </div>
                <div className="muted" style={{ marginTop: 4, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {summaryModelLabel(revision)}{revision.tldr ? ` · ${revision.tldr}` : ''}
                </div>
                {revision.status === 'failed' && revision.error_detail && (
                  <div style={{ marginTop: 4, fontSize: 11, color: 'var(--danger-tx)' }}>{summaryErrorLabel(revision.error_code ?? revision.error_detail ?? 'SUMMARY_GENERATION_FAILED')}</div>
                )}
              </div>
              {!revision.is_current && revision.content && activeRevision?.content && (
                <button className="btn btn-ghost sm" onClick={() => setCompareRevisionId(revision.id)}>
                  {tr('比较', 'Compare')}
                </button>
              )}
              {canManage && !revision.is_current && (revision.status === 'ready' || revision.status === 'stale') && !softDeleted && (
                <button className="btn btn-ghost sm" disabled={!!running || activate.isPending} onClick={() => setActivateRevisionId(revision.id)}>
                  {tr('设为当前', 'Activate')}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <Modal
        open={!!compareRevision && !!activeRevision}
        onClose={() => setCompareRevisionId(null)}
        title={tr('比较总结版本', 'Compare summary revisions')}
        width={900}
        footer={<button className="btn btn-ghost sm" onClick={() => setCompareRevisionId(null)}>{tr('关闭', 'Close')}</button>}
      >
        {compareRevision && activeRevision && (
          <div className="settings-2col">
            <div>
              <div className="mono muted" style={{ fontSize: 10.5, marginBottom: 6 }}>
                {tr('当前版本', 'Current')} · {fmtTime(activeRevision.created_at)}
              </div>
              <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 520, overflow: 'auto', fontSize: 11.5, lineHeight: 1.55 }}>{activeRevision.content}</pre>
            </div>
            <div>
              <div className="mono muted" style={{ fontSize: 10.5, marginBottom: 6 }}>
                {tr('对比版本', 'Compared')} · {fmtTime(compareRevision.created_at)}
              </div>
              <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 520, overflow: 'auto', fontSize: 11.5, lineHeight: 1.55 }}>{compareRevision.content}</pre>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmModal
        open={!!activateRevisionId}
        onClose={() => setActivateRevisionId(null)}
        title={tr('切换通用总结版本', 'Change the shared summary revision')}
        message={tr(
          '这会改变所有收录该论文的文献库当前展示的总结，历史版本不会丢失。',
          'This changes the current summary in every library containing this paper. Revision history is preserved.',
        )}
        confirmText={tr('切换版本', 'Change revision')}
        busy={activate.isPending}
        onConfirm={() => activateRevisionId && activate.mutate(activateRevisionId)}
      />
      <ConfirmModal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={tr('删除通用论文总结', 'Delete the shared paper summary')}
        message={tr(
          '总结会从所有文献库隐藏并进入 30 天回收站；论文、PDF 和 Zotero 数据不受影响。',
          'The summary will be hidden in every library and kept in the 30-day trash. The paper, PDF, and Zotero data are unaffected.',
        )}
        confirmText={tr('移入回收站', 'Move to trash')}
        danger
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
