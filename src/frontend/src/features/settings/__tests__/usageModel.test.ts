import { describe, expect, it } from 'vitest';
import type { LlmUsageRow } from '../../../lib/api';
import { cacheHitRate, formatUsd, pricingFromDraft, sumUsd, summarizeUsage, usageByModel } from '../usageModel';

const row = (patch: Partial<LlmUsageRow> = {}): LlmUsageRow => ({
  date: '2026-09-21', stage: 'agent', provider_name: 'Provider A', model: 'model-a',
  prompt_tokens: 1000, completion_tokens: 200, cache_read_tokens: 600, cache_creation_tokens: 100,
  cache_reported_calls: 1, estimated_calls: 0, priced_calls: 1, cost_usd: '0.004', calls: 1,
  ...patch,
});

describe('model usage aggregation', () => {
  it('counts cached input once and uses all input as the hit-rate denominator', () => {
    const totals = summarizeUsage([
      row(),
      row({ prompt_tokens: 1000, cache_read_tokens: 0, cache_creation_tokens: 0, cache_reported_calls: 0, priced_calls: 0, cost_usd: null, estimated_calls: 1 }),
    ]);
    expect(totals.prompt_tokens).toBe(2000);
    expect(totals.cache_read_tokens).toBe(600);
    expect(totals.cache_creation_tokens).toBe(100);
    expect(cacheHitRate(totals)).toBe(0.3);
    expect(totals.cache_reported_calls).toBe(1);
    expect(totals.estimated_calls).toBe(1);
    expect(totals.calls).toBe(2);
    expect(totals.priced_calls).toBe(1);
    expect(totals.cost_usd).toBe('0.004');
  });

  it('keeps identical model IDs at different providers separate', () => {
    const groups = usageByModel([row(), row({ date: '2026-09-20' }), row({ provider_name: 'Provider B' })]);
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ provider_name: 'Provider A', calls: 2, prompt_tokens: 2000 });
    expect(groups[1]).toMatchObject({ provider_name: 'Provider B', calls: 1 });
  });

  it('distinguishes missing usage from a reported zero hit rate', () => {
    expect(cacheHitRate(row({ prompt_tokens: 0 }))).toBeNull();
    expect(cacheHitRate(row({ cache_reported_calls: 0, cache_read_tokens: 0 }))).toBeNull();
    expect(cacheHitRate(row({ cache_read_tokens: 0 }))).toBe(0);
    expect(cacheHitRate(row({ cache_reported_calls: 0, cache_read_tokens: 300 }))).toBe(0.3);
    expect(summarizeUsage([]).cost_usd).toBeNull();
  });

  it('sums Decimal costs exactly, including scientific notation and tiny amounts', () => {
    expect(sumUsd(['0.1', '0.2', null])).toBe('0.3');
    expect(sumUsd(['0E-12', '1E-8', '0.00000002'])).toBe('0.000000030000');
    expect(sumUsd(['1.1E2', '2'])).toBe('112');
    expect(sumUsd([null, null])).toBeNull();
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd('0')).toBe('$0.00');
    expect(formatUsd('0.00000001')).toBe('< $0.000001');
  });
});

describe('model pricing input', () => {
  const draft = { model: ' model-a ', input: ' 2 ', output: '8', cacheRead: '', cacheCreation: '0' };
  it('preserves unknown cache prices separately from explicitly free prices', () => {
    expect(pricingFromDraft(draft)).toEqual({
      input_per_million: '2', output_per_million: '8', cache_read_per_million: null, cache_creation_per_million: '0',
    });
  });
  it('rejects missing, negative, nonnumeric, and nonfinite prices', () => {
    for (const input of ['', '-1', 'NaN', 'Infinity', '1,200', '2USD', '1000000.01', '0.123456789']) {
      expect(pricingFromDraft({ ...draft, input })).toBeNull();
    }
    expect(pricingFromDraft({ ...draft, model: ' ' })).toBeNull();
    expect(pricingFromDraft({ ...draft, cacheRead: '-1' })).toBeNull();
  });
});
