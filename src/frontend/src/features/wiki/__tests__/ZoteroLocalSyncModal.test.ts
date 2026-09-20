import { describe, expect, it } from 'vitest';

import { ApiError } from '../../../lib/api';
import { isBlockingZoteroBindingError } from '../ZoteroLocalSyncModal';

describe('Zotero binding error safety', () => {
  it('treats only an explicit 404 as an unbound library', () => {
    expect(isBlockingZoteroBindingError(new ApiError(404, 'not found'))).toBe(false);
    expect(isBlockingZoteroBindingError(new ApiError(500, 'database unavailable'))).toBe(true);
    expect(isBlockingZoteroBindingError(new Error('network unavailable'))).toBe(true);
  });
});
