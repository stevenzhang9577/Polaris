import { chromium } from 'playwright-core';

/** Keep browser E2E out of Codex Seatbelt, where Chromium can crash after tests exit. */
export function testBrowserOptions(env: NodeJS.ProcessEnv = process.env) {
  if (env.CODEX_SANDBOX === 'seatbelt') {
    throw new Error('Browser E2E is unsafe inside Codex Seatbelt: Chromium can SIGTRAP after the test exits. ' +
      'Run this single E2E command with require_escalated or from a regular terminal.');
  }
  const executablePath = env.POLARIS_TEST_BROWSER || undefined;
  return {
    headless: true,
    executablePath,
  };
}

export function launchTestBrowser() {
  return chromium.launch(testBrowserOptions());
}
