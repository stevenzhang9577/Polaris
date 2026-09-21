import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { api, type LlmUsageRow } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { fmtFullTime } from '../../lib/format';
import { stageLabel } from '../../lib/stageLabels';
import { cacheHitRate, formatUsd, summarizeUsage, usageByModel, type UsageTotals } from './usageModel';
import { ActiveModelRoute } from './ActiveModelRoute';
import './usage.css';

function coverage(reported: number, calls: number): string {
  return `${reported.toLocaleString()} / ${calls.toLocaleString()}`;
}

function rateLabel(usage: UsageTotals): string {
  const rate = cacheHitRate(usage);
  return rate === null ? '—' : `${(rate * 100).toFixed(1)}%`;
}

export function UsageCost({ usage }: { usage: UsageTotals }) {
  const known = usage.priced_calls > 0 && usage.cost_usd !== null;
  return (
    <>
      <span className="mono">{formatUsd(known ? usage.cost_usd : usage.reference_cost_usd ?? null)}</span>
      {usage.reference_cost_usd != null && <small className="usage-subtext">{tr('CC Switch 参考 ', 'CC Switch reference ')}{formatUsd(usage.reference_cost_usd)}</small>}
      {usage.calls > 0 && usage.priced_calls < usage.calls && usage.reference_cost_usd == null && (
        <small className="usage-subtext">{known
          ? tr(`部分已计价 · ${coverage(usage.priced_calls, usage.calls)} 次`, `Partial · ${coverage(usage.priced_calls, usage.calls)} calls priced`)
          : tr('费用未知', 'Cost unknown')}</small>
      )}
    </>
  );
}

function CacheTokens({ usage }: { usage: UsageTotals }) {
  if (!usage.cache_reported_calls && !usage.cache_read_tokens && !usage.cache_creation_tokens) return <span title={tr('供应商未上报完整缓存明细', 'Complete cache details were not reported')}>—</span>;
  const read = usage.cache_reported_calls || usage.cache_read_tokens ? usage.cache_read_tokens.toLocaleString() : '—';
  const creation = usage.cache_reported_calls || usage.cache_creation_tokens ? usage.cache_creation_tokens.toLocaleString() : '—';
  return (
    <>
      <span className="mono">{read} / {creation}</span>
      {usage.cache_reported_calls < usage.calls && (
        <small className="usage-subtext">{tr('完整上报 ', 'Fully reported ')}{coverage(usage.cache_reported_calls, usage.calls)}</small>
      )}
    </>
  );
}

export function UsageReport({ rows }: { rows: LlmUsageRow[] }) {
  const totals = summarizeUsage(rows);
  const models = usageByModel(rows);
  return (
    <>
      <div className="settings-stats usage-stats">
        <div className="card card-pad">
          <span className="usage-stat-label">{tr('全部输入', 'Total input')}</span>
          <strong className="mono usage-stat-value">{totals.prompt_tokens.toLocaleString()}</strong>
          <span className="usage-subtext">{tr('tokens · 包含缓存读取与写入', 'tokens · includes cache reads and writes')}</span>
        </div>
        <div className="card card-pad">
          <span className="usage-stat-label">{tr('输出', 'Output')}</span>
          <strong className="mono usage-stat-value">{totals.completion_tokens.toLocaleString()}</strong>
          <span className="usage-subtext">{tr(`${totals.calls.toLocaleString()} 次调用`, `${totals.calls.toLocaleString()} calls`)}</span>
        </div>
        <div className="card card-pad">
          <span className="usage-stat-label">{tr('缓存读取 / 命中率', 'Cache reads / hit rate')}</span>
          <strong className="mono usage-stat-value">{totals.cache_reported_calls || totals.cache_read_tokens ? totals.cache_read_tokens.toLocaleString() : '—'}</strong>
          <span className="usage-subtext">{rateLabel(totals)}{tr(' · 读取 tokens / 全部输入', ' · read tokens / all input')}</span>
          <span className="usage-subtext">{tr('缓存完整上报 ', 'Complete cache reporting ')}{coverage(totals.cache_reported_calls, totals.calls)}{tr(' 次', ' calls')}</span>
        </div>
        <div className="card card-pad">
          <span className="usage-stat-label">{tr('估算费用 · USD', 'Estimated cost · USD')}</span>
          <div className="usage-stat-value"><UsageCost usage={totals} /></div>
          <span className="usage-subtext">{tr('按调用时保存的单价计算', 'Calculated with prices saved at call time')}</span>
        </div>
      </div>
      <p className="usage-note">
        {tr('缓存已经包含在输入中，不重复计入总量；缓存命中率按已记录的读取量除以全部输入计算。', 'Cache tokens are included in input, so they are not added again. The hit rate is recorded cache reads divided by all input.')}
        {totals.cache_reported_calls < totals.calls && tr(
          ' 部分调用未上报缓存明细；读取量只含已知值，命中率仍以全部输入为分母，因此是保守下界。',
          ' Some calls did not report cache details; reads include known values only, while the rate still uses all input as its denominator and is therefore a conservative lower bound.',
        )}
        {totals.estimated_calls > 0 && tr(` ${totals.estimated_calls.toLocaleString()} 次调用的 token 数为估算。`, ` Token counts were estimated for ${totals.estimated_calls.toLocaleString()} calls.`)}
        {' '}{tr('费用为估算值，未计价调用不按免费处理。', 'Costs are estimates; unpriced calls are not treated as free.')}
        {' '}{tr('历史供应商缺失表示旧版本没有保存归属，不能用当前配置倒推。CC Switch 参考费用使用本机当前单价，缺失的缓存明细按无折扣估算；未匹配价格的模型不计入参考合计。', 'Missing historical providers were not recorded by older versions and cannot be inferred from current settings. CC Switch reference costs use current local prices without discounts for unknown cache usage; models without matching prices are excluded.')}
      </p>
      <div className="card card-pad usage-section">
        <div className="section-h">{tr('按模型汇总', 'Usage by model')}</div>
        <div className="table-wrap">
          <table className="table usage-table">
            <thead><UsageHeaders /></thead>
            <tbody>{models.map((row) => (
              <tr key={JSON.stringify([row.provider_name, row.model])}>
                <td>{row.provider_name ?? tr('历史未记录', 'Not recorded historically')}</td>
                <td className="mono">{row.model}</td>
                <UsageCells usage={row} />
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>
      <div className="card card-pad usage-section">
        <div className="section-h">{tr('每日汇总', 'Daily totals')}</div>
        <div className="table-wrap">
          <table className="table usage-table">
            <thead><UsageHeaders detail /></thead>
            <tbody>{rows.map((row, index) => (
              <tr key={index}>
                <td className="mono">{row.date}</td>
                <td>{tr(stageLabel(row.stage).zh, stageLabel(row.stage).en)}</td>
                <td>{row.provider_name ?? tr('历史未记录', 'Not recorded historically')}</td>
                <td className="mono">{row.model}</td>
                <UsageCells usage={row} />
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function UsageHeaders({ detail = false }: { detail?: boolean }) {
  return <tr>
    {detail && <><th>{tr('日期 (UTC)', 'Date (UTC)')}</th><th>{tr('环节', 'Stage')}</th></>}
    <th>{tr('供应商', 'Provider')}</th><th>{tr('模型', 'Model')}</th>
    <th className="usage-number">{tr('全部输入', 'All input')}</th>
    <th className="usage-number">{tr('输出', 'Output')}</th>
    <th className="usage-number">{tr('缓存读取 / 写入', 'Cache read / write')}</th>
    <th className="usage-number">{tr('命中率', 'Hit rate')}</th>
    <th className="usage-number">{tr('调用', 'Calls')}</th>
    <th className="usage-number">{tr('估算费用 (USD)', 'Estimated cost (USD)')}</th>
  </tr>;
}

function UsageCells({ usage }: { usage: UsageTotals }) {
  return <>
    <td className="usage-number mono">{usage.prompt_tokens.toLocaleString()}</td>
    <td className="usage-number mono">{usage.completion_tokens.toLocaleString()}</td>
    <td className="usage-number"><CacheTokens usage={usage} /></td>
    <td className="usage-number mono">{rateLabel(usage)}</td>
    <td className="usage-number mono">{usage.calls.toLocaleString()}</td>
    <td className="usage-number"><UsageCost usage={usage} /></td>
  </>;
}

export function UsageDashboard({ scope }: { scope: 'personal' | 'platform' }) {
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const [model, setModel] = useState('');
  const query = useQuery({
    queryKey: ['llm-usage', scope, days],
    queryFn: () => scope === 'personal' ? api.myUsageHistory({ days: Number(days) }) : api.getLlmUsage({ days: Number(days) }),
    retry: false,
  });
  const rows = query.data ?? [];
  const models = useMemo(() => [...new Set((query.data ?? []).map((row) => row.model))].sort(), [query.data]);
  const selectedRows = rows.filter((row) => !model || row.model === model);
  return (
    <>
      {scope === 'platform' && <ActiveModelRoute />}
      <div className="usage-toolbar">
        <div className="section-h"><Icon name="chart" size={16} style={{ color: 'var(--accent)' }} />
          {scope === 'personal' ? tr('我的模型用量', 'My model usage') : tr('模型用量总览', 'Model usage overview')}
        </div>
        <div className="row gap8 wrap">
          <SelectMenu value={model} options={[{ value: '', label: tr('全部模型', 'All models') }, ...models.map((value) => ({ value, label: value })), ...(model && !models.includes(model) ? [{ value: model, label: model }] : [])]}
            wrapStyle={{ width: 240 }} onChange={setModel} />
          <Segmented options={[{ v: '7' as const, label: tr('7 天', '7 days') }, { v: '30' as const, label: tr('30 天', '30 days') }, { v: '90' as const, label: tr('90 天', '90 days') }]}
            value={days} onChange={setDays} />
          <button className="btn btn-soft sm" onClick={() => void query.refetch()} disabled={query.isFetching}>{tr('刷新', 'Refresh')}</button>
        </div>
      </div>
      {query.isLoading ? <div className="empty">{tr('加载中…', 'Loading…')}</div>
        : query.isError ? <div className="empty">{tr('无法加载用量，请重试。', 'Could not load usage. Please retry.')}</div>
        : selectedRows.length === 0 ? <div className="card empty">{tr(`近 ${days} 天暂无匹配的用量记录`, `No matching usage records in the last ${days} days`)}</div>
        : <UsageReport rows={selectedRows} />}
      <UsageCalls scope={scope} days={Number(days)} model={model} key={`${scope}:${days}:${model}`} />
    </>
  );
}

function UsageCalls({ scope, days, model }: { scope: 'personal' | 'platform'; days: number; model: string }) {
  const [page, setPage] = useState(0);
  const query = useQuery({
    queryKey: ['llm-usage-calls', scope, days, model, page],
    queryFn: () => api.getUsageCalls(scope, days, model, page * 50),
    refetchInterval: 10000,
  });
  return <div className="card card-pad usage-section">
    <div className="section-h">{tr('逐次调用明细 · 本地时间', 'Individual calls · local time')}</div>
    {query.isError ? <button className="btn btn-soft sm" onClick={() => void query.refetch()}>{tr('重试加载', 'Retry')}</button> :
      <div className="table-wrap"><table className="table usage-table">
        <thead><tr><th>{tr('时间', 'Time')}</th><th>{tr('环节', 'Stage')}</th><th>{tr('供应商', 'Provider')}</th><th>{tr('模型', 'Model')}</th>
          <th>{tr('输入', 'Input')}</th><th>{tr('输出', 'Output')}</th><th>{tr('缓存读 / 写', 'Cache read / write')}</th><th>{tr('命中率', 'Hit rate')}</th><th>{tr('调用', 'Calls')}</th><th>USD</th></tr></thead>
        <tbody>{query.data?.items.map(row => <tr key={row.id}>
          <td className="mono" style={{ whiteSpace: 'nowrap' }}>{fmtFullTime(row.occurred_at)}</td>
          <td>{tr(stageLabel(row.stage).zh, stageLabel(row.stage).en)}</td><td>{row.provider_name ?? tr('历史未记录', 'Not recorded historically')}</td><td className="mono">
            <div>{tr('返回：', 'Returned: ')}{row.model}</div>
            {row.requested_model && <div className="muted">{tr('请求：', 'Requested: ')}{row.requested_model}</div>}
            {row.requested_model && row.requested_model !== row.model && <div style={{ color: 'var(--warn-tx)' }}>{tr('请求与返回名称不同，请核对网关映射', 'Request and response names differ; check gateway mapping')}</div>}
            {row.pricing_model && <div className="muted">{tr('计价依据：', 'Price basis: ')}{row.pricing_model}</div>}
            {row.response_error && <div style={{ color: 'var(--danger-tx)' }}>{tr('响应协议不匹配', 'Response protocol mismatch')}</div>}
          </td><UsageCells usage={row} />
        </tr>)}</tbody>
      </table></div>}
    <div className="row gap8" style={{ marginTop: 12 }}>
      <button className="btn btn-soft sm" disabled={!page || query.isFetching} onClick={() => setPage(page - 1)}>{tr('上一页', 'Previous')}</button>
      <span>{page + 1} / {Math.max(1, Math.ceil((query.data?.total ?? 0) / 50))}</span>
      <button className="btn btn-soft sm" disabled={query.isFetching || (page + 1) * 50 >= (query.data?.total ?? 0)} onClick={() => setPage(page + 1)}>{tr('下一页', 'Next')}</button>
    </div>
  </div>;
}
