const { spawnSync } = require('node:child_process');
const path = require('node:path');

if (process.env.CODEX_SANDBOX === 'seatbelt') {
  console.error('Browser E2E is unsafe inside Codex Seatbelt. Run the single E2E command with require_escalated or from a regular terminal.');
  process.exit(1);
}

const playwrightRoot = path.dirname(require.resolve('playwright-core/package.json'));
const result = spawnSync(process.execPath, [path.join(playwrightRoot, 'cli.js'), 'install', 'chromium-headless-shell'], {
  stdio: 'inherit',
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
