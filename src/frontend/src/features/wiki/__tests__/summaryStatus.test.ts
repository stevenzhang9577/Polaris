import { describe, expect, it } from 'vitest';
import type { PaperSummaryRevision } from '../../../lib/api';
import { latestSummaryFailure, summaryModelLabel, summaryUtcTime } from '../summaryStatus';

const row = (status: string, created_at: string, extra = {}) => ({ status, created_at, ...extra } as PaperSummaryRevision);
describe('summary attempt provenance', () => {
  it('does not show an old error above a newer successful summary', () => {
    const failed = row('failed', '2026-09-20T22:43:00');
    const ready = row('ready', '2026-09-21T00:03:00');
    expect(latestSummaryFailure([failed, ready])).toBeUndefined();
    const recent = row('failed', '2026-09-21T01:00:00');
    expect(latestSummaryFailure([ready, failed, recent])).toBe(recent);
    expect(latestSummaryFailure([row('failed', '2026-09-21T02:00:00', { error_code: 'SUMMARY_CANCELLED' }), ready, failed])).toBeUndefined();
  });
  it('distinguishes requested, returned and missing historical model names', () => {
    const text = summaryModelLabel(row('failed', '', { requested_model: 'kimi-k3[1M]', model: 'glm-5.3-flash' }));
    expect(text).toContain('kimi-k3[1M]');
    expect(text).toContain('glm-5.3-flash');
    expect(summaryModelLabel(row('generating', '', { requested_model: 'kimi-k3[1M]' }))).toContain('kimi-k3[1M]');
    expect(summaryModelLabel(row('failed', ''))).toContain('未保存');
  });
  it('interprets legacy SQLite timestamps as UTC before local formatting', () => {
    expect(summaryUtcTime('2026-09-21T04:59:00')).toBe('2026-09-21T04:59:00Z');
    expect(summaryUtcTime('2026-09-21T12:59:00+08:00')).toBe('2026-09-21T12:59:00+08:00');
  });
});
