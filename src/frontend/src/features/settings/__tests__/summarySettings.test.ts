import { describe, expect, it } from 'vitest';
import { validSummaryConcurrency } from '../SummarySettingsPanel';

describe('summary concurrency settings', () => {
  it('accepts the default and both limits', () => {
    for (const value of ['1', '3', '10', '20']) expect(validSummaryConcurrency(value)).toBe(true);
  });
  it('rejects missing, non-numeric, fractional and out-of-bounds values', () => {
    for (const value of ['', ' ', 'NaN', 'three', '0', '-1', '21', '3.5', 'Infinity']) {
      expect(validSummaryConcurrency(value), value).toBe(false);
    }
  });
});
