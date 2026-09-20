import type { SummaryBatchFilters, SummaryBatchInput } from '../../lib/api';

export type SummarySelection = Pick<SummaryBatchInput, 'paper_ids' | 'filters' | 'excluded_ids'>;

export function summarySelectionPayload(
  allFiltered: boolean,
  selected: ReadonlySet<string>,
  excluded: ReadonlySet<string>,
  filters: SummaryBatchFilters,
): SummarySelection {
  return allFiltered
    ? { filters: { ...filters }, excluded_ids: [...excluded] }
    : { paper_ids: [...selected] };
}

export function selectedPaperCount(allFiltered: boolean, total: number, selected: ReadonlySet<string>, excluded: ReadonlySet<string>): number {
  return allFiltered ? Math.max(0, total - excluded.size) : selected.size;
}

export function isPaperSelected(id: string, allFiltered: boolean, selected: ReadonlySet<string>, excluded: ReadonlySet<string>): boolean {
  return allFiltered ? !excluded.has(id) : selected.has(id);
}

export function togglePaperId(ids: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(ids);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}
