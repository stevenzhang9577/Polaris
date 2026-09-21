import { ApiError } from './api';

export function isTransientReadError(error: unknown): boolean {
  return error instanceof ApiError
    ? error.status >= 500 || error.status === 408 || error.status === 429
    : error instanceof TypeError;
}

/** Only apply to reads: automatically replaying a task-creation mutation is unsafe. */
export const readQueryOptions = {
  retry: (failureCount: number, error: unknown) => failureCount < 2 && isTransientReadError(error),
  retryDelay: (attempt: number) => Math.min(1000 * 2 ** attempt, 5000),
  // An exhausted transient failure should recover while the page remains open.
  refetchInterval: (query: { state: { error: unknown } }) =>
    isTransientReadError(query.state.error) ? 10_000 : false as const,
};
