import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api, type SummaryBatch, type SummaryBatchItem } from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import type { SummarySelection } from './summarySelection';

export function summaryBatchFinished(batch: SummaryBatch): number {
  return batch.completed + batch.skipped + batch.failed;
}

export function summaryBatchStatus(batch: SummaryBatch): string {
  if (batch.status === 'paused') return batch.running > 0 ? tr('暂停中，等待当前论文完成', 'Pausing; finishing active papers') : tr('已暂停', 'Paused');
  const labels: Record<SummaryBatch['status'], [string, string]> = {
    queued: ['排队中', 'Queued'], running: ['生成中', 'Generating'], paused: ['已暂停', 'Paused'],
    completed: ['已完成', 'Completed'], completed_with_errors: ['已结束，有失败项', 'Finished with errors'],
  };
  const label = labels[batch.status];
  return label ? tr(...label) : `${tr('未知状态', 'Unknown status')} (${batch.status})`;
}

export function summaryBatchItemLabel(item: SummaryBatchItem): string {
  if (item.status === 'running') {
    const stages: Record<string, [string, string]> = {
      materialize: ['获取全文', 'Getting full text'], parse: ['解析论文', 'Parsing'],
      compile: ['生成总结', 'Generating'], project: ['同步工作区', 'Syncing workspace'],
    };
    return item.stage && stages[item.stage] ? tr(...stages[item.stage]!) : tr('处理中', 'In progress');
  }
  const labels: Record<SummaryBatchItem['status'], [string, string]> = {
    pending: ['等待中', 'Waiting'], running: ['处理中', 'In progress'], completed: ['已完成', 'Completed'],
    skipped: ['已跳过', 'Skipped'], failed: ['失败', 'Failed'],
  };
  const label = labels[item.status];
  return label ? tr(...label) : `${tr('未知状态', 'Unknown status')} (${item.status})`;
}

export function summaryBatchErrorLabel(code: string): string {
  const labels: Record<string, [string, string]> = {
    LLM_NOT_CONFIGURED: ['尚未配置可用的模型；任务已暂停，请完成模型配置后重试失败项', 'No model is configured. The batch was paused; configure a model and retry failed items.'],
    LLM_PROVIDER_TIMEOUT: ['模型服务响应超时；任务已暂停，请检查服务后重试失败项', 'The model service timed out. The batch was paused; check it and retry failed items.'],
    LLM_PROVIDER_UNAVAILABLE: ['模型服务无法连接；任务已暂停，请启动或检查模型服务后重试失败项', 'The model service could not be reached. The batch was paused; start or check it and retry failed items.'],
    LLM_PROVIDER_REQUEST_FAILED: ['模型接口请求失败；任务已暂停，请检查模型与路由配置后重试失败项', 'The model request failed. The batch was paused; check model routing and retry failed items.'],
    SUMMARY_DATABASE_TRANSACTION_FAILED: ['本地数据库事务失败；该论文可单独重试', 'The local database transaction failed. This paper can be retried.'],
    SUMMARY_GENERATION_FAILED: ['总结生成失败；请重试，若仍失败请查看日志', 'Summary generation failed. Retry it and check logs if it fails again.'],
  };
  const label = labels[code];
  return label ? tr(...label) : code;
}

export function SummaryBatchProgress({ batch, compact = false }: { batch: SummaryBatch; compact?: boolean }) {
  const done = summaryBatchFinished(batch);
  const percent = batch.total ? Math.min(100, Math.round(done / batch.total * 100)) : 100;
  return (
    <div style={{ minWidth: 0 }}>
      <div className="row gap8 wrap" style={{ justifyContent: 'space-between', fontSize: compact ? 11.5 : 13 }}>
        <strong>{summaryBatchStatus(batch)}</strong>
        <span className="muted">{done} / {batch.total} · {percent}%</span>
      </div>
      <progress aria-label={tr('论文总结进度', 'Summary progress')} value={done} max={batch.total || 1}
        style={{ display: 'block', width: '100%', height: 7, margin: '9px 0', accentColor: 'var(--accent)' }} />
      {!compact && (
        <div className="row gap8 wrap" style={{ fontSize: 12, lineHeight: 1.7 }}>
          <span>{tr(`等待 ${batch.pending}`, `Waiting ${batch.pending}`)}</span>
          <span>{tr(`进行中 ${batch.running}`, `Active ${batch.running}`)}</span>
          <span style={{ color: 'var(--ok-tx)' }}>{tr(`完成 ${batch.completed}`, `Completed ${batch.completed}`)}</span>
          <span className="muted">{tr(`跳过 ${batch.skipped}`, `Skipped ${batch.skipped}`)}</span>
          <span style={{ color: batch.failed ? 'var(--danger-tx)' : undefined }}>{tr(`失败 ${batch.failed}`, `Failed ${batch.failed}`)}</span>
        </div>
      )}
    </div>
  );
}

export function SummaryBatchPanel({ libraryId, selection, selectedCount, onCloseCreate }: {
  libraryId: string;
  selection: SummarySelection | null;
  selectedCount: number;
  onCloseCreate: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [skipExisting, setSkipExisting] = useState(true);
  const requestId = useRef(crypto.randomUUID());
  const batches = useQuery({
    queryKey: ['summary-batches', libraryId],
    queryFn: () => api.listSummaryBatches(libraryId),
    retry: false,
    refetchInterval: (query) => query.state.data?.some((batch) => batch.running > 0 || batch.status === 'queued' || batch.status === 'running') ? 2_000 : 10_000,
  });
  const settings = useQuery({
    queryKey: ['summary-settings'],
    queryFn: () => api.getSummarySettings(),
    enabled: !!selection,
    retry: false,
  });
  const detail = useQuery({
    queryKey: ['summary-batch', libraryId, batchId, page],
    queryFn: () => api.getSummaryBatch(libraryId, batchId!, page),
    enabled: historyOpen && !!batchId,
    retry: false,
    refetchInterval: historyOpen && batchId ? 2_000 : false,
  });
  const current = detail.data?.batch;
  const latest = batches.data?.[0];
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['summary-batches', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['summary-batch', libraryId] });
  };
  // A selection snapshot owns its retry ID. Retrying a lost response never creates a second batch.
  useEffect(() => {
    if (!selection) return;
    requestId.current = crypto.randomUUID();
    setSkipExisting(true);
  }, [selection]);
  const completion = (batches.data ?? []).map((batch) => `${batch.id}:${batch.completed}`).join(',');
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ['papers', libraryId] });
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
    void queryClient.invalidateQueries({ queryKey: ['paper-summary'] });
    void queryClient.invalidateQueries({ queryKey: ['paper-summary-history'] });
  }, [completion, libraryId, queryClient]);
  const create = useMutation({
    mutationFn: () => {
      if (!selection) throw new Error('SELECTION_REQUIRED');
      return api.createSummaryBatch(libraryId, { ...selection, request_id: requestId.current, skip_existing: skipExisting });
    },
    onSuccess: (batch) => {
      refresh();
      onCloseCreate();
      setBatchId(batch.id);
      setPage(1);
      setHistoryOpen(true);
      toast(tr('总结任务已创建，关闭窗口后仍会继续', 'Summary batch created; it continues after closing this dialog'), 'ok');
    },
    onError: (error) => toast(`${tr('创建失败，可重试', 'Could not create batch; retry is safe')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const control = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'retry') => api.controlSummaryBatch(libraryId, batchId!, action),
    onSuccess: refresh,
    onError: (error) => toast(`${tr('操作失败', 'Action failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const showHistory = () => {
    setBatchId(latest?.id ?? null);
    setPage(1);
    setHistoryOpen(true);
  };

  return (
    <>
      <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
        <div className="row gap8 wrap" style={{ justifyContent: 'space-between', marginBottom: latest ? 8 : 0 }}>
          <span style={{ fontSize: 12, fontWeight: 600 }}>{tr('批量论文总结', 'Batch summaries')}</span>
          <button className="btn btn-soft sm" onClick={showHistory}><Icon name="clock" size={12} />{tr('总结任务', 'Summary tasks')}</button>
        </div>
        {latest && <SummaryBatchProgress batch={latest} compact />}
        {batches.isError && <div role="status" className="muted" style={{ fontSize: 11 }}>{tr('任务状态暂时不可用', 'Task status unavailable')} <button className="btn btn-ghost sm" onClick={() => void batches.refetch()}>{tr('重试', 'Retry')}</button></div>}
      </div>

      <Modal open={!!selection} onClose={() => { if (!create.isPending) onCloseCreate(); }}
        title={tr('批量生成论文总结', 'Generate paper summaries')}
        sub={tr(`已选择 ${selectedCount} 篇论文`, `${selectedCount} papers selected`)}
        footer={<>
          <button className="btn btn-ghost sm" disabled={create.isPending} onClick={onCloseCreate}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary sm" disabled={create.isPending || selectedCount === 0 || !settings.data || settings.isError} onClick={() => create.mutate()}>
            <Icon name="sparkle" size={13} />{create.isPending ? tr('创建任务中…', 'Creating…') : tr('开始生成', 'Start generation')}
          </button>
        </>}
      >
        <div style={{ fontSize: 13, lineHeight: 1.8 }}>
          {selection?.filters && <p style={{ marginTop: 0 }}>{tr('包含当前筛选条件下的全部论文，不仅是已加载的页面。点击开始时会固定这一批论文，之后新导入的论文不会自动加入。', 'Includes all matching papers, not just loaded pages. The batch is fixed when started; later imports are not added.')}</p>}
          <label className="row gap8" style={{ alignItems: 'flex-start' }}>
            <input type="checkbox" checked={skipExisting} disabled={create.isPending} onChange={(event) => { setSkipExisting(event.target.checked); requestId.current = crypto.randomUUID(); }} style={{ marginTop: 6 }} />
            <span>{tr('跳过已有总结的论文', 'Skip papers with an existing summary')}<br /><span className="muted" style={{ fontSize: 12 }}>{tr('默认开启；关闭后会生成新历史版本，不删除原版本。', 'Enabled by default. Regenerating adds a revision and preserves existing history.')}</span></span>
          </label>
          <p className="muted">{tr('优先读取已有 PDF 全文；没有可用全文时会生成明确标注的摘要级解读。此操作会调用已配置的模型接口，可能产生费用。', 'Uses PDF full text when available; otherwise creates a clearly labeled abstract-level summary. This calls your configured model and may incur charges.')}</p>
          {settings.isError ? <div role="alert">{tr('无法读取并发设置', 'Could not load concurrency settings')} <button className="btn btn-soft sm" onClick={() => void settings.refetch()}>{tr('重试', 'Retry')}</button></div> : <p>{settings.isLoading ? tr('读取并发设置中…', 'Loading concurrency…') : tr(`同时处理最多 ${settings.data?.concurrency} 篇，其余排队。`, `Up to ${settings.data?.concurrency} papers at a time; others are queued.`)}</p>}
          <button className="btn btn-soft sm" disabled={create.isPending} onClick={() => { onCloseCreate(); navigate('/settings?tab=summaries'); }}><Icon name="settings" size={13} />{tr('前往设置调整并发', 'Change concurrency in Settings')}</button>
        </div>
      </Modal>

      <Modal open={historyOpen} onClose={() => setHistoryOpen(false)} title={tr('论文总结任务', 'Paper summary tasks')}
        sub={tr('任务会持久保存；关闭本窗口不取消任务。暂停只停止派发新论文。', 'Tasks are persisted. Closing this dialog does not cancel them. Pausing stops new papers from starting.')}
        width={800} footer={<button className="btn btn-primary sm" onClick={() => setHistoryOpen(false)}>{tr('完成', 'Done')}</button>}
      >
        {batches.isLoading ? <p>{tr('读取任务中…', 'Loading tasks…')}</p> : batches.isError ? <button className="btn btn-soft sm" onClick={() => void batches.refetch()}>{tr('重试加载任务', 'Retry loading tasks')}</button> : !batches.data?.length ? <p className="muted">{tr('还没有批量总结任务。在论文列表全选或勾选后，点击「生成总结」。', 'No summary batches yet. Select papers in the list and choose Generate summaries.')}</p> : <>
          <label className="col gap6" style={{ fontSize: 12, marginBottom: 18 }}>{tr('最近任务', 'Recent tasks')}
            <select className="input" value={batchId ?? ''} onChange={(event) => { setBatchId(event.target.value); setPage(1); }}>
              {!batchId && <option value="">{tr('选择任务', 'Select a task')}</option>}
              {batches.data.map((batch) => <option key={batch.id} value={batch.id}>{fmtTime(batch.created_at)} · {batch.total} {tr('篇', 'papers')} · {summaryBatchStatus(batch)}</option>)}
            </select>
          </label>
          {detail.isLoading ? <p>{tr('读取进度中…', 'Loading progress…')}</p> : detail.isError ? <div role="alert">{tr('无法读取任务进度', 'Could not load progress')} <button className="btn btn-soft sm" onClick={() => void detail.refetch()}>{tr('重试', 'Retry')}</button></div> : current && <>
            <SummaryBatchProgress batch={current} />
            <div className="row gap8 wrap" style={{ margin: '14px 0' }}>
              {(current.status === 'running' || current.status === 'queued') && <button className="btn btn-soft sm" disabled={control.isPending} onClick={() => control.mutate('pause')}>{tr('暂停任务', 'Pause')}</button>}
              {current.status === 'paused' && <button className="btn btn-primary sm" disabled={control.isPending} onClick={() => control.mutate('resume')}>{tr('继续任务', 'Resume')}</button>}
              {current.failed > 0 && <button className="btn btn-soft sm" disabled={control.isPending} onClick={() => control.mutate('retry')}>{tr(`重试失败 ${current.failed} 篇`, `Retry ${current.failed} failed`)}</button>}
              <span className="muted" style={{ fontSize: 12 }}>{tr(`当前并发上限 ${current.concurrency}`, `Current concurrency limit ${current.concurrency}`)}</span>
            </div>
            <div style={{ borderTop: '1px solid var(--border)' }}>
              {detail.data?.items.map((item) => <div key={item.paper_id} style={{ padding: '10px 0', borderBottom: '1px solid var(--border)', fontSize: 12 }}>
                <div className="row gap8" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <span style={{ overflowWrap: 'anywhere', minWidth: 0 }}>{item.title}</span>
                  <span className="pill sm" style={{ flexShrink: 0, color: item.status === 'failed' ? 'var(--danger-tx)' : undefined }}>{summaryBatchItemLabel(item)}</span>
                </div>
                {item.error && <div style={{ marginTop: 4, color: item.status === 'failed' ? 'var(--danger-tx)' : 'var(--text-3)', overflowWrap: 'anywhere' }}>{summaryBatchErrorLabel(item.error)}</div>}
              </div>)}
            </div>
            {detail.data && detail.data.total > detail.data.size && <div className="row gap8 wrap" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
              <button className="btn btn-soft sm" disabled={page === 1 || detail.isFetching} onClick={() => setPage((value) => value - 1)}>{tr('上一页', 'Previous')}</button>
              <span style={{ fontSize: 12 }}>{page} / {Math.ceil(detail.data.total / detail.data.size)}</span>
              <button className="btn btn-soft sm" disabled={page * detail.data.size >= detail.data.total || detail.isFetching} onClick={() => setPage((value) => value + 1)}>{tr('下一页', 'Next')}</button>
            </div>}
          </>}
        </>}
      </Modal>
    </>
  );
}
