import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { SummaryBatch, SummaryBatchFilters, SummaryBatchItem } from '../../../lib/api';
import { isPaperSelected, selectedPaperCount, summarySelectionPayload, togglePaperId } from '../summarySelection';
import {
  SummaryBatchProgress,
  summaryBatchErrorLabel,
  summaryBatchFinished,
  summaryBatchItemLabel,
  summaryBatchStatus,
} from '../SummaryBatchPanel';

const filters: SummaryBatchFilters = {
  status: 'library', q: 'adversarial', sort: 'relevance', starred: true, my_tag: 'read next',
  reading_status: 'unread', author: 'Zhang', affiliation: 'University',
  published_from: '2020-01-01T00:00:00Z', published_to: '2026-01-01T23:59:59Z',
  created_from: '2026-01-01T00:00:00Z', created_to: '2026-02-01T23:59:59Z', last_sync_only: true,
};
const batch: SummaryBatch = {
  id: 'batch-1', library_id: 'library-1', status: 'running', total: 7000,
  pending: 6887, running: 3, completed: 100, skipped: 8, failed: 2, concurrency: 3,
  created_at: '2026-09-21T00:00:00Z', updated_at: '2026-09-21T00:00:00Z',
};

describe('summary all-pages selection', () => {
  it('sends the full filter rather than just the twenty loaded IDs', () => {
    const selected = new Set(Array.from({ length: 20 }, (_, index) => `paper-${index}`));
    const payload = summarySelectionPayload(true, selected, new Set(), filters);
    expect(payload).toEqual({ filters, excluded_ids: [] });
    expect(payload).not.toHaveProperty('paper_ids');
    expect(payload.filters).not.toHaveProperty('page');
    expect(payload.filters).not.toHaveProperty('size');
    expect(selectedPaperCount(true, 7000, selected, new Set())).toBe(7000);
  });

  it('supports deselecting individual papers without enumerating all IDs', () => {
    const excluded = togglePaperId(new Set<string>(), 'paper-8');
    expect(isPaperSelected('paper-8', true, new Set(), excluded)).toBe(false);
    expect(isPaperSelected('unloaded-paper', true, new Set(), excluded)).toBe(true);
    expect(selectedPaperCount(true, 7000, new Set(), excluded)).toBe(6999);
    expect(summarySelectionPayload(true, new Set(), excluded, filters).excluded_ids).toEqual(['paper-8']);
    expect(togglePaperId(excluded, 'paper-8').size).toBe(0);
  });

  it('manual or bounded semantic selection sends only explicit IDs', () => {
    const ids = new Set(['paper-4', 'paper-29']);
    expect(summarySelectionPayload(false, ids, new Set(), filters)).toEqual({ paper_ids: ['paper-4', 'paper-29'] });
    expect(selectedPaperCount(false, 7000, ids, new Set())).toBe(2);
    expect(isPaperSelected('paper-30', false, ids, new Set())).toBe(false);
  });

  it('freezes a filter snapshot without mutating the source sets', () => {
    const selected = new Set(['paper-1']);
    const next = togglePaperId(selected, 'paper-2');
    expect([...selected]).toEqual(['paper-1']);
    expect(next.size).toBe(2);
    const payload = summarySelectionPayload(true, selected, new Set(), filters);
    expect(payload.filters).not.toBe(filters);
  });
});

describe('summary task progress', () => {
  it('counts completed, skipped and failed as processed but not running', () => {
    expect(summaryBatchFinished(batch)).toBe(110);
    const markup = renderToStaticMarkup(<SummaryBatchProgress batch={batch} />);
    expect(markup).toContain('value="110"');
    expect(markup).toContain('max="7000"');
    expect(markup).toContain('进行中 3');
    expect(markup).toContain('跳过 8');
    expect(markup).toContain('失败 2');
  });

  it('distinguishes waiting for active papers from fully paused', () => {
    expect(summaryBatchStatus({ ...batch, status: 'paused' })).toContain('等待当前论文完成');
    expect(summaryBatchStatus({ ...batch, status: 'paused', running: 0 })).toBe('已暂停');
  });

  it('makes partial completion explicit', () => {
    expect(summaryBatchStatus({ ...batch, status: 'completed_with_errors' })).toContain('失败项');
  });

  it('keeps progress visible for an unknown persisted batch status', () => {
    const unknown = { ...batch, status: 'unexpected' } as unknown as SummaryBatch;
    expect(summaryBatchStatus(unknown)).toBe('未知状态 (unexpected)');
    expect(renderToStaticMarkup(<SummaryBatchProgress batch={unknown} />)).toContain('未知状态 (unexpected)');
  });

  it('shows unknown item status and stage safely', () => {
    const item = { paper_id: 'paper-1', title: 'Paper', status: 'future_status', stage: null, error: null } as unknown as SummaryBatchItem;
    expect(summaryBatchItemLabel(item)).toBe('未知状态 (future_status)');
    expect(summaryBatchItemLabel({ ...item, status: 'running', stage: 'future_stage' })).toBe('处理中');
  });

  it('turns provider failure codes into actionable guidance', () => {
    expect(summaryBatchErrorLabel('LLM_PROVIDER_UNAVAILABLE')).toContain('任务已暂停');
    expect(summaryBatchErrorLabel('FUTURE_ERROR')).toBe('FUTURE_ERROR');
  });
});
