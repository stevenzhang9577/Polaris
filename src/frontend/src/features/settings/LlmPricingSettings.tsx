import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FormField } from '../../components/ui/FormField';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { api, type LlmProviderRead, type ModelPricing } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { pricingFromDraft, type PricingDraft } from './usageModel';
import './usage.css';

function draftFor(model: string, pricing?: ModelPricing): PricingDraft {
  return {
    model,
    input: pricing?.input_per_million ?? '',
    output: pricing?.output_per_million ?? '',
    cacheRead: pricing?.cache_read_per_million ?? '',
    cacheCreation: pricing?.cache_creation_per_million ?? '',
  };
}

function ProviderPricing({ provider }: { provider: LlmProviderRead }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<PricingDraft>(draftFor(''));
  const price = pricingFromDraft(draft);
  const prices = provider.model_pricing ?? {};
  const modelOptions = [...new Set([...(provider.models ?? []), ...Object.keys(prices)])].sort();
  const mutation = useMutation({
    mutationFn: (modelPricing: Record<string, ModelPricing>) => api.patchLlmProvider(provider.id, { model_pricing: modelPricing }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['llm', 'providers'] });
      toast(tr('模型单价已保存，将用于新调用', 'Model prices saved for future calls'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${error instanceof Error ? error.message : String(error)}`, 'error'),
  });
  const importPrices = useMutation({
    mutationFn: async () => {
      const catalogue = await api.getCcSwitchPricing();
      const imported: Record<string, ModelPricing> = {};
      for (const model of modelOptions) {
        const match = catalogue[model.trim().toLowerCase().replace(/\[\d+[mk]\]$/, '')];
        if (match) imported[model] = match;
      }
      if (!Object.keys(imported).length) throw new Error(tr('CC Switch 没有匹配的单价', 'No matching CC Switch prices'));
      // Preserve explicitly configured provider-specific rates.
      return api.patchLlmProvider(provider.id, { model_pricing: { ...imported, ...prices } });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['llm', 'providers'] });
      toast(tr('已导入匹配单价，已有单价保留', 'Matching prices imported; existing prices preserved'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), 'error'),
  });
  return (
    <>
      <p className="usage-note">
        {tr('单价单位为 USD / 百万 tokens。输入单价用于未命中缓存的输入；缓存读取和写入按各自单价计算。缓存价格留空表示未知，填 0 表示该项免费。', 'Prices are USD per million tokens. The input price applies to uncached input; cache reads and writes use their own prices. An empty cache price means unknown; enter 0 only when it is free.')}
        {' '}{tr('保存只影响之后的调用，历史费用保留当时的单价。', 'Saved prices apply to future calls only. Historical costs keep the prices recorded at call time.')}
      </p>
      <button className="btn btn-soft sm" disabled={importPrices.isPending} onClick={() => importPrices.mutate()}>{tr('从本机 CC Switch 导入匹配单价', 'Import matching prices from local CC Switch')}</button>
      <form onSubmit={(event) => {
        event.preventDefault();
        if (price) mutation.mutate({ ...prices, [draft.model.trim()]: price });
      }}>
        <FormField label={tr('模型 ID', 'Model ID')} hint={tr('与调用使用的模型 ID 完全一致', 'Must exactly match the model ID used in calls')}>
          <input className="input mono" list={`pricing-models-${provider.id}`} aria-label={tr('模型 ID', 'Model ID')} value={draft.model} placeholder={tr('选择或输入模型 ID', 'Select or enter a model ID')}
            onChange={(event) => setDraft(draftFor(event.target.value, prices[event.target.value]))} />
          <datalist id={`pricing-models-${provider.id}`}>{modelOptions.map((model) => <option value={model} key={model} />)}</datalist>
        </FormField>
        <div className="usage-pricing-fields">
          {([
            { key: 'input', zh: '输入单价', en: 'Input price' },
            { key: 'output', zh: '输出单价', en: 'Output price' },
            { key: 'cacheRead', zh: '缓存读取单价（可选）', en: 'Cache read price (optional)' },
            { key: 'cacheCreation', zh: '缓存写入单价（可选）', en: 'Cache write price (optional)' },
          ] as const).map((field) => (
            <FormField key={field.key} label={tr(field.zh, field.en)}>
              <input className="input mono" type="text" inputMode="decimal" aria-label={tr(field.zh, field.en)} value={draft[field.key]} placeholder="USD / 1M"
                onChange={(event) => setDraft({ ...draft, [field.key]: event.target.value })} />
            </FormField>
          ))}
        </div>
        <div className="row gap12 wrap" style={{ marginBottom: 14 }}>
          <button className="btn btn-primary sm" type="submit" disabled={!price || mutation.isPending}>{mutation.isPending ? tr('保存中…', 'Saving…') : tr('保存模型单价', 'Save model prices')}</button>
          <span className="usage-subtext">{tr('输入和输出必填；单价为 0–1,000,000，最多 8 位小数。', 'Input and output are required; prices must be 0–1,000,000 with up to 8 decimal places.')}</span>
        </div>
      </form>
      {Object.keys(prices).length === 0 ? (
        <div className="empty" style={{ padding: 18 }}>{tr('尚未配置单价，用量将记录为费用未知。', 'No prices configured. Usage costs will be recorded as unknown.')}</div>
      ) : (
        <div className="table-wrap">
          <table className="table usage-table">
            <thead><tr>
              <th>{tr('模型', 'Model')}</th><th>{tr('输入', 'Input')}</th><th>{tr('输出', 'Output')}</th>
              <th>{tr('缓存读取', 'Cache read')}</th><th>{tr('缓存写入', 'Cache write')}</th><th>{tr('操作', 'Actions')}</th>
            </tr></thead>
            <tbody>{Object.entries(prices).sort(([a], [b]) => a.localeCompare(b)).map(([model, pricing]) => (
              <tr key={model}>
                <td className="mono">{model}</td>
                <td className="mono">{pricing.input_per_million}</td><td className="mono">{pricing.output_per_million}</td>
                <td className="mono">{pricing.cache_read_per_million ?? '—'}</td><td className="mono">{pricing.cache_creation_per_million ?? '—'}</td>
                <td><div className="row gap8">
                  <button type="button" className="btn btn-soft sm" onClick={() => setDraft(draftFor(model, pricing))}>{tr('编辑', 'Edit')}</button>
                  <button type="button" className="btn btn-soft sm" disabled={mutation.isPending} onClick={() => {
                    const next = { ...prices };
                    delete next[model];
                    mutation.mutate(next);
                  }}>{tr('移除单价', 'Remove prices')}</button>
                </div></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function LlmPricingSettings() {
  const [providerId, setProviderId] = useState('');
  const query = useQuery({ queryKey: ['llm', 'providers'], queryFn: () => api.listLlmProviders(), retry: false });
  const provider = query.data?.find((item) => item.id === providerId) ?? query.data?.[0];
  return (
    <div className="card card-pad usage-section">
      <div className="row gap12 wrap" style={{ justifyContent: 'space-between' }}>
        <div className="section-h">{tr('模型单价', 'Model pricing')} <span className="en-label">USD / 1M tokens</span></div>
        {provider && <SelectMenu value={provider.id} options={(query.data ?? []).map((item) => ({ value: item.id, label: item.name }))}
          wrapStyle={{ width: 260 }} onChange={setProviderId} />}
      </div>
      {query.isLoading ? <div className="empty">{tr('加载中…', 'Loading…')}</div>
        : query.isError ? <div className="empty">{tr('无法加载供应商', 'Could not load providers')}{' '}<button className="btn btn-soft sm" onClick={() => void query.refetch()}>{tr('重试', 'Retry')}</button></div>
        : !provider ? <div className="empty">{tr('先添加供应商，再配置模型单价。', 'Add a provider before configuring model prices.')}</div>
        : <ProviderPricing provider={provider} key={provider.id} />}
    </div>
  );
}
