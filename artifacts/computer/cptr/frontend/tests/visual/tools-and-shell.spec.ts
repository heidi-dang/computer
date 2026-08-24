import { expect, test } from '@playwright/test';

const fixtures = [
	{ id: 'tools-surface', heading: 'Tools' },
	{ id: 'tool-servers-surface', heading: 'Tool Servers' },
	{ id: 'terminal-surface', heading: 'Shell' },
	{ id: 'tool-call-surface', heading: 'run_command' }
] as const;

test.beforeEach(async ({ page }) => {
	await page.route('**/api/admin/config', (route) =>
		route.fulfill({ json: { config: { 'tool_approval.default_builtin_approval': 'review' } } })
	);
	await page.route('**/api/admin/tools/approval', (route) =>
		route.fulfill({
			json: {
				default_approval: 'review',
				overrides: {},
				groups: [
					{
						id: 'terminal',
						tools: [
							{ name: 'run_command', default_approval: 'review' },
							{ name: 'run_command_with_timeout', default_approval: 'review' }
						]
					},
					{ id: 'files', tools: [{ name: 'read_file', default_approval: 'allow' }] }
				]
			}
		})
	);
	await page.route('**/api/admin/tools/servers', (route) =>
		route.fulfill({
			json: {
				servers: [
					{
						id: 'docs',
						type: 'openapi',
						url: 'https://example.com/api',
						path: 'openapi.json',
						auth_type: 'none',
						key: '',
						name: 'Documentation API',
						description: 'A connected documentation source',
						headers: null,
						enabled: true
					}
				]
			}
		})
	);
});

for (const fixture of fixtures) {
	test(`${fixture.heading} stays visible without horizontal overflow`, async ({ page }) => {
		await page.goto('/__visual-regression');
		const surface = page.getByTestId(fixture.id);
		await expect(surface).toBeVisible();
		await expect(surface.getByText(fixture.heading, { exact: false }).first()).toBeVisible();

		const overflow = await surface.evaluate((element) => element.scrollWidth > element.clientWidth);
		expect(overflow, `${fixture.heading} surface is horizontally clipped`).toBe(false);
	});
}

test('status and action affordances remain visible at narrow width', async ({ page }) => {
	await page.goto('/__visual-regression');
	await expect(page.getByTestId('tool-servers-surface').getByText('Available')).toBeVisible();
	await expect(page.getByTestId('tool-call-surface').getByText('Done')).toBeVisible();
	await expect(page.getByTestId('terminal-surface').getByText('Live session')).toBeVisible();
});

test('captures stable desktop and narrow surface snapshots', async ({ page }) => {
	await page.goto('/__visual-regression');
	await expect(page.getByTestId('tools-surface')).toHaveScreenshot('tools-surface.png', {
		animations: 'disabled',
		caret: 'hide'
	});
	await expect(page.getByTestId('tool-servers-surface')).toHaveScreenshot(
		'tool-servers-surface.png',
		{
			animations: 'disabled',
			caret: 'hide'
		}
	);
	await expect(page.getByTestId('tool-call-surface')).toHaveScreenshot('tool-call-surface.png', {
		animations: 'disabled',
		caret: 'hide'
	});
});
