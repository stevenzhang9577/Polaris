/** Keep the installed Chrome app out of automated tests unless explicitly requested. */
export function testBrowserOptions(env: NodeJS.ProcessEnv = process.env) {
  const executablePath = env.POLARIS_TEST_BROWSER || undefined;
  if (env.CODEX_SANDBOX === 'seatbelt' && executablePath &&
      /\.app\/Contents\/MacOS\//i.test(executablePath)) {
    throw new Error('POLARIS_TEST_BROWSER points to a macOS app that Codex Seatbelt cannot launch. ' +
      'Unset it to use Playwright headless shell, or run this test outside the sandbox.');
  }
  return {
    headless: true,
    executablePath,
    ...(env.CODEX_SANDBOX === 'seatbelt' ? {
      // The headless shell needs these process settings inside Codex's macOS sandbox.
      args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-gpu', '--disable-dev-shm-usage'],
    } : {}),
  };
}

export function launchTestBrowser() {
  const options = testBrowserOptions();
  if (process.env.CODEX_SANDBOX === 'seatbelt' && !process.env.PLAYWRIGHT_BROWSERS_PATH) {
    // Playwright resolves this at import time. Its package-local cache is writable in the workspace.
    process.env.PLAYWRIGHT_BROWSERS_PATH = '0';
  }
  const { chromium } = require('playwright-core') as typeof import('playwright-core');
  return chromium.launch(options);
}
