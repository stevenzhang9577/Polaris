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

test('rejects a macOS app override inside Codex seatbelt', () => {
  assert.throws(() => testBrowserOptions({
    CODEX_SANDBOX: 'seatbelt',
    POLARIS_TEST_BROWSER: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  }), /Unset it to use Playwright headless shell/);
});

test('uses verified headless-shell flags inside Codex seatbelt', () => {
  assert.deepEqual(testBrowserOptions({ CODEX_SANDBOX: 'seatbelt' }), {
    headless: true,
    executablePath: undefined,
    args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-gpu', '--disable-dev-shm-usage'],
  });
});
