const { spawnSync } = require('node:child_process');
const path = require('node:path');

const env = { ...process.env };
if (env.CODEX_SANDBOX === 'seatbelt' && !env.PLAYWRIGHT_BROWSERS_PATH) {
  env.PLAYWRIGHT_BROWSERS_PATH = '0';
}

const playwrightRoot = path.dirname(require.resolve('playwright-core/package.json'));
const result = spawnSync(process.execPath, [path.join(playwrightRoot, 'cli.js'), 'install', 'chromium-headless-shell'], {
  env,
  stdio: 'inherit',
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
