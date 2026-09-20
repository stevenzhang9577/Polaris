import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';

export function validSummaryConcurrency(value: string): boolean {
  const number = Number(value);
  return value.trim() !== '' && Number.isInteger(number) && number >= 1 && number <= 10;
}

export function SummarySettingsPanel() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const settings = useQuery({
    queryKey: ['summary-settings'],
    queryFn: () => api.getSummarySettings(),
    retry: false,
  });
  const shown = draft ?? String(settings.data?.concurrency ?? 3);
  const valid = validSummaryConcurrency(shown);
  const save = useMutation({
    mutationFn: () => api.putSummarySettings({ concurrency: Number(shown) }),
    onSuccess: (result) => {
      queryClient.setQueryData(['summary-settings'], result);
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ['summary-batches'] });
      void queryClient.invalidateQueries({ queryKey: ['summary-batch'] });
      toast(tr('论文总结并发数已保存', 'Summary concurrency saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });

  return (
    <section className="card card-pad" aria-labelledby="summary-settings-title">
      <h2 id="summary-settings-title" className="section-h"><Icon name="sparkle" size={16} />{tr('论文总结', 'Paper summaries')}</h2>
      <p className="muted" style={{ fontSize: 13, lineHeight: 1.7, margin: '10px 0 20px' }}>
        {tr('控制你同时生成总结的论文数量，单篇与批量任务共用额度。其余论文自动排队，不会一次请求全部模型接口。', 'Limit the papers you summarize at the same time. Single and batch tasks share this limit; other papers wait in the queue.')}
      </p>
      {settings.isLoading ? <p className="muted">{tr('读取设置中…', 'Loading settings…')}</p> : settings.isError ? (
        <div role="alert" className="row gap8 wrap">
          <span>{tr('无法读取总结设置', 'Could not load summary settings')}</span>
          <button className="btn btn-soft sm" onClick={() => void settings.refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      ) : (
        <>
          <div className="row gap12 wrap" style={{ alignItems: 'center' }}>
            <label htmlFor="summary-concurrency" style={{ fontSize: 13, fontWeight: 600 }}>{tr('同时生成的论文数', 'Concurrent papers')}</label>
            <input id="summary-concurrency" className="input" type="number" min={1} max={10} step={1}
              value={shown} onChange={(event) => setDraft(event.target.value)} disabled={save.isPending}
              aria-describedby="summary-concurrency-help" aria-invalid={!valid} style={{ width: 90 }} />
            <button className="btn btn-primary sm" disabled={!valid || save.isPending || Number(shown) === settings.data?.concurrency} onClick={() => save.mutate()}>
              {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </div>
          <p id="summary-concurrency-help" className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>
            {tr('范围 1–10，默认 3。建议从较低并发开始，避免触发模型服务限流。降低并发不会中断正在生成的论文；新的任务会按新额度启动。', 'Range 1–10; default 3. Start low to avoid provider rate limits. Lowering the limit does not interrupt running papers; new tasks use the updated limit.')}
          </p>
          {!valid && <p role="alert" style={{ fontSize: 12, color: 'var(--danger-tx)' }}>{tr('请输入 1 到 10 之间的整数。', 'Enter an integer from 1 to 10.')}</p>}
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: 16, fontSize: 13, lineHeight: 1.7 }}>
            {tr('到文献库 → 论文库，点击「全选筛选结果」或逐篇勾选，再点「生成总结」。默认跳过已有总结，任务支持暂停、继续和失败重试。', 'In Library → Papers, select all filtered results or individual papers, then choose Generate summaries. Existing summaries are skipped by default. Batches support pause, resume, and retry.')}
          </div>
        </>
      )}
    </section>
  );
}
