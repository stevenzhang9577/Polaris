import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';

vi.mock('../endpoint', () => ({ apiBase: () => '/api', serverOrigin: () => '' }));
vi.mock('../token-store', () => ({ readToken: () => 'test-token', writeToken: () => undefined }));
vi.mock('../local-session', () => ({ handleUnauthorized: () => undefined }));
afterEach(() => vi.unstubAllGlobals());

describe('summary batch client protocol', () => {
  it('posts a filter selection with exclusions and an idempotency ID', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'batch-1' })));
    vi.stubGlobal('fetch', fetcher);
    const input = { request_id: 'request-1', filters: { status: 'library' as const, q: 'robustness' }, excluded_ids: ['paper-1'], skip_existing: true };
    await api.createSummaryBatch('library-1', input);
    const [url, options] = fetcher.mock.calls[0]!;
    expect(url).toBe('/api/libraries/library-1/summary-batches');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body)).toEqual(input);
    expect(options.headers.get('Authorization')).toBe('Bearer test-token');
  });

  it('reads paginated progress and exposes all three control actions', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{}'));
    vi.stubGlobal('fetch', fetcher);
    await api.getSummaryBatch('library-1', 'batch-1', 2, 20);
    expect(fetcher.mock.calls[0]![0]).toBe('/api/libraries/library-1/summary-batches/batch-1?page=2&size=20');
    for (const action of ['pause', 'resume', 'retry'] as const) {
      await api.controlSummaryBatch('library-1', 'batch-1', action);
      const [url, options] = fetcher.mock.calls.at(-1)!;
      expect(url).toBe(`/api/libraries/library-1/summary-batches/batch-1/${action}`);
      expect(options.method).toBe('POST');
    }
  });

  it('saves user-level summary concurrency through the settings endpoint', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{"concurrency":5}'));
    vi.stubGlobal('fetch', fetcher);
    expect(await api.putSummarySettings({ concurrency: 5 })).toEqual({ concurrency: 5 });
    const [url, options] = fetcher.mock.calls[0]!;
    expect(url).toBe('/api/summary-settings');
    expect(options.method).toBe('PUT');
    expect(JSON.parse(options.body)).toEqual({ concurrency: 5 });
  });
});
