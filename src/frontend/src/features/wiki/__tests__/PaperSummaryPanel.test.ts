import { describe, expect, it } from 'vitest';
import type { PaperSummaryRevision } from '../../../lib/api';
import { isSummaryRevisionInFlight } from '../PaperSummaryPanel';

function revision(
  status: PaperSummaryRevision['status'],
  stage: PaperSummaryRevision['stage'],
): PaperSummaryRevision {
  return {
    id: 'revision-1',
    paper_id: 'paper-1',
    content_version_id: null,
    source_level: 'abstract',
    content: null,
    tldr: null,
    model: null,
    prompt_version: null,
    schema_version: null,
    created_by: null,
    source_fingerprint: null,
    evidence_manifest: null,
    status,
    stage,
    error_code: null,
    error_detail: null,
    is_current: false,
    created_at: '2026-09-20T00:00:00Z',
    updated_at: '2026-09-20T00:00:00Z',
  };
}

describe('isSummaryRevisionInFlight', () => {
  it('keeps polling while a ready revision is still projecting', () => {
    expect(isSummaryRevisionInFlight(revision('ready', 'project'))).toBe(true);
  });

  it('stops polling after a ready revision completes projection', () => {
    expect(isSummaryRevisionInFlight(revision('ready', 'complete'))).toBe(false);
  });

  it('stops polling after a revision fails', () => {
    expect(isSummaryRevisionInFlight(revision('failed', 'compile'))).toBe(false);
  });
});
