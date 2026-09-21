import type { LlmUsageRow, ModelPricing } from '../../lib/api';

export type UsageTotals = Omit<LlmUsageRow, 'date' | 'stage' | 'model' | 'provider_name'>;
export type ModelUsage = UsageTotals & Pick<LlmUsageRow, 'model' | 'provider_name'>;

/** Add Decimal API values without rounding each request to cents. */
export function sumUsd(values: Array<string | null>): string | null {
  const parts = values.filter((value): value is string => value !== null).map((value) => {
    const [coefficient = '0', exponent = '0'] = value.toLowerCase().split('e');
    const [whole = '0', fraction = ''] = coefficient.split('.');
    const scale = fraction.length - Number(exponent);
    const units = BigInt(whole + fraction);
    return scale < 0
      ? { units: units * 10n ** BigInt(-scale), scale: 0 }
      : { units, scale };
  });
  if (!parts.length) return null;
  const scale = Math.max(...parts.map((part) => part.scale));
  const total = parts.reduce((sum, part) => sum + part.units * 10n ** BigInt(scale - part.scale), 0n);
  if (!scale) return total.toString();
  const digits = total.toString().padStart(scale + 1, '0');
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
}

export function summarizeUsage(rows: UsageTotals[]): UsageTotals {
  return {
    prompt_tokens: rows.reduce((sum, row) => sum + row.prompt_tokens, 0),
    completion_tokens: rows.reduce((sum, row) => sum + row.completion_tokens, 0),
    cache_read_tokens: rows.reduce((sum, row) => sum + row.cache_read_tokens, 0),
    cache_creation_tokens: rows.reduce((sum, row) => sum + row.cache_creation_tokens, 0),
    cache_reported_calls: rows.reduce((sum, row) => sum + row.cache_reported_calls, 0),
    estimated_calls: rows.reduce((sum, row) => sum + row.estimated_calls, 0),
    priced_calls: rows.reduce((sum, row) => sum + row.priced_calls, 0),
    calls: rows.reduce((sum, row) => sum + row.calls, 0),
    cost_usd: sumUsd(rows.map((row) => row.cost_usd)),
  };
}

export function usageByModel(rows: LlmUsageRow[]): ModelUsage[] {
  const groups = new Map<string, LlmUsageRow[]>();
  for (const row of rows) {
    const key = JSON.stringify([row.provider_name, row.model]);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups.values()].map((group) => ({
    ...summarizeUsage(group),
    provider_name: group[0]!.provider_name,
    model: group[0]!.model,
  })).sort((a, b) => b.prompt_tokens + b.completion_tokens - a.prompt_tokens - a.completion_tokens);
}

export function cacheHitRate(usage: UsageTotals): number | null {
  return usage.prompt_tokens > 0 && (usage.cache_reported_calls > 0 || usage.cache_read_tokens > 0)
    ? usage.cache_read_tokens / usage.prompt_tokens
    : null;
}

export function formatUsd(value: string | null): string {
  if (value === null) return '—';
  const amount = Number(value);
  if (amount > 0 && amount < 0.000001) return '< $0.000001';
  return `$${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`;
}

export interface PricingDraft {
  model: string;
  input: string;
  output: string;
  cacheRead: string;
  cacheCreation: string;
}

export function pricingFromDraft(draft: PricingDraft): ModelPricing | null {
  const valid = (value: string) => /^\d+(?:\.\d{1,8})?$/.test(value.trim()) && Number(value) <= 1_000_000;
  if (!draft.model.trim() || !valid(draft.input) || !valid(draft.output)) return null;
  if (draft.cacheRead.trim() && !valid(draft.cacheRead)) return null;
  if (draft.cacheCreation.trim() && !valid(draft.cacheCreation)) return null;
  return {
    input_per_million: draft.input.trim(),
    output_per_million: draft.output.trim(),
    cache_read_per_million: draft.cacheRead.trim() || null,
    cache_creation_per_million: draft.cacheCreation.trim() || null,
  };
}
