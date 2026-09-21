import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { ApiError } from '../api';
import { readQueryOptions } from '../read-query';

describe('transient read recovery', () => {
  it('recovers a failed read without losing its cached data', async () => {
    const client = new QueryClient();
    let failing = true;
    const observer = new QueryObserver(client, {
      queryKey: ['papers'],
      queryFn: async () => {
        if (failing) throw new ApiError(503, 'busy');
        return ['paper-a'];
      },
      ...readQueryOptions,
      retryDelay: 1,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    try {
      const failed = await observer.refetch();
      expect(failed.isError).toBe(true);
      expect(readQueryOptions.refetchInterval({ state: { error: failed.error } })).toBe(10_000);
      failing = false;
      expect((await observer.refetch()).data).toEqual(['paper-a']);
      failing = true;
      const refresh = await observer.refetch();
      expect(refresh.isError).toBe(true);
      expect(refresh.data).toEqual(['paper-a']);
    } finally {
      unsubscribe();
      client.clear();
    }
  });

  it('retries network and server failures, but not authorization or missing resources', () => {
    for (const status of [408, 429, 500, 503]) {
      expect(readQueryOptions.retry(0, new ApiError(status, 'failed'))).toBe(true);
      expect(readQueryOptions.retry(2, new ApiError(status, 'failed'))).toBe(false);
    }
    expect(readQueryOptions.retry(0, new TypeError('Failed to fetch'))).toBe(true);
    for (const status of [400, 401, 403, 404]) {
      const error = new ApiError(status, 'failed');
      expect(readQueryOptions.retry(0, error)).toBe(false);
      expect(readQueryOptions.refetchInterval({ state: { error } })).toBe(false);
    }
    expect(readQueryOptions.refetchInterval({ state: { error: null } })).toBe(false);
  });
});
