import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests/visual',
	timeout: 30_000,
	fullyParallel: true,
	use: {
		baseURL: 'http://127.0.0.1:4175',
		trace: 'retain-on-failure',
		launchOptions: {
			executablePath: process.env.CHROMIUM_PATH
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
