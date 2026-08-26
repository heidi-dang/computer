import { execFileSync } from 'node:child_process';
import { defineConfig, devices } from '@playwright/test';

function resolveChromiumPath(): string | undefined {
	if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
	try {
		return execFileSync('sh', ['-lc', 'command -v chromium || command -v chromium-browser'], {
			encoding: 'utf8'
		}).trim() || undefined;
	} catch {
		return undefined;
	}
}

export default defineConfig({
	testDir: './tests/visual',
	timeout: 30_000,
	expect: { timeout: 15_000 },
	fullyParallel: true,
	use: {
		baseURL: 'http://127.0.0.1:4175',
		trace: 'retain-on-failure',
		launchOptions: {
			executablePath: resolveChromiumPath()
		}
	},
	webServer: {
		command: 'npm run dev -- --host 127.0.0.1 --port 4175',
		url: 'http://127.0.0.1:4175/__visual-regression',
		reuseExistingServer: true
	},
	projects: [
		{
			name: 'desktop',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } }
		},
		{ name: 'narrow', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } }
	]
});
