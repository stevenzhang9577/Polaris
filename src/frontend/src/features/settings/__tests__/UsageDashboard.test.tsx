import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { LlmProviderRead, LlmUsageRow } from '../../../lib/api';
import { LlmPricingSettings } from '../LlmPricingSettings';
import { settingsTabFromParam } from '../SettingsPage';
import { UsageDashboard, UsageReport } from '../UsageDashboard';

const row: LlmUsageRow = {
  date: '2026-09-21', stage: 'agent', provider_name: 'Provider A', model: 'model-a',
  prompt_tokens: 1000, completion_tokens: 200, cache_read_tokens: 500, cache_creation_tokens: 100,
  cache_reported_calls: 1, estimated_calls: 1, priced_calls: 1, cost_usd: '0.125', calls: 2,
};

function renderDashboard(scope: 'personal' | 'platform'): string {
  const client = new QueryClient();
  client.setQueryData(['llm-usage', scope, '30'], [row]);
  return renderToStaticMarkup(<QueryClientProvider client={client}><UsageDashboard scope={scope} /></QueryClientProvider>);
}

describe('usage reporting', () => {
  it('renders cache coverage and marks the priced subset incomplete', () => {
    const html = renderToStaticMarkup(<UsageReport rows={[row]} />);
    for (const text of ['全部输入', '输出', '缓存读取 / 命中率', '50.0%', '缓存完整上报 ', '部分已计价', '$0.125', 'Provider A', 'model-a', '按模型汇总', '每日明细', '未计价调用不按免费处理']) {
      expect(html).toContain(text);
    }
    expect(html).toContain('1 次调用的 token 数为估算');
  });

  it('does not display an unknown cost as zero or imply unknown cache means no hits', () => {
    const html = renderToStaticMarkup(<UsageReport rows={[{ ...row, priced_calls: 0, cost_usd: null, cache_reported_calls: 0, cache_read_tokens: 0, cache_creation_tokens: 0 }]} />);
    expect(html).toContain('费用未知');
    expect(html).not.toContain('$0.00');
    expect(html).not.toContain('0.0%');
    expect(html).toContain('部分调用未上报缓存明细');
  });

  it('uses the shared data presentation for both personal and platform pages', () => {
    for (const scope of ['personal', 'platform'] as const) {
      const html = renderDashboard(scope);
      expect(html).toContain('全部模型');
      expect(html).toContain('7 天');
      expect(html).toContain('30 天');
      expect(html).toContain('90 天');
      expect(html).toContain('model-a');
      expect(html).toContain('$0.125');
    }
  });

  it('resolves direct links to pricing and platform usage', () => {
    expect(settingsTabFromParam('llm')).toBe('llm');
    expect(settingsTabFromParam('usage')).toBe('usage');
    expect(settingsTabFromParam('myusage')).toBe('myusage');
    expect(settingsTabFromParam('unknown')).toBe('personal');
    expect(settingsTabFromParam(null)).toBe('personal');
  });
});

describe('model pricing display', () => {
  it('shows configured provider prices and makes the future-only boundary explicit', () => {
    const provider: LlmProviderRead = {
      id: 'provider-a', name: 'Provider A', kind: 'openai_compat', transport: 'chat_completions', auth_scheme: 'bearer',
      base_url: null, user_agent: null, api_key_masked: null, enabled: true, models: ['model-a'],
      import_source: null, import_source_key: null, import_fingerprint: null, imported_at: null,
      model_pricing: { 'model-a': { input_per_million: '2', output_per_million: '8', cache_read_per_million: null, cache_creation_per_million: '0' } },
    };
    const client = new QueryClient();
    client.setQueryData(['llm', 'providers'], [provider]);
    const html = renderToStaticMarkup(<QueryClientProvider client={client}><LlmPricingSettings /></QueryClientProvider>);
    expect(html).toContain('Provider A');
    expect(html).toContain('model-a');
    expect(html).toContain('保存只影响之后的调用');
    expect(html).toContain('缓存价格留空表示未知');
    expect(html).toContain('填 0 表示该项免费');
    expect(html).toContain('type="button"');
  });
});
