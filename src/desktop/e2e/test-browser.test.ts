import assert from 'node:assert/strict';
import { test } from 'node:test';

import { testBrowserOptions } from './test-browser';

test('uses Playwright headless shell by default', () => {
  assert.deepEqual(testBrowserOptions({}), { headless: true, executablePath: undefined });
});

test('honors an explicitly selected browser', () => {
  assert.deepEqual(testBrowserOptions({ POLARIS_TEST_BROWSER: '/test/chrome' }), {
    headless: true,
    executablePath: '/test/chrome',
  });
});

test('rejects any browser launch inside Codex seatbelt', () => {
  assert.throws(() => testBrowserOptions({
    CODEX_SANDBOX: 'seatbelt',
    POLARIS_TEST_BROWSER: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  }), /Browser E2E is unsafe inside Codex Seatbelt/);
  assert.throws(() => testBrowserOptions({ CODEX_SANDBOX: 'seatbelt' }),
    /Run this single E2E command with require_escalated/);
});
