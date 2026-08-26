import { expect, test } from '@playwright/test';

const forbiddenDetails = [
	'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
	'/srv/flowdeck/private/credentials.json',
	'Bearer super-secret-token',
	'Error: private exception text'
];

test.beforeEach(async ({ page }) => {
	await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/auth', (route) =>
		route.fulfill({
			json: {
				authenticated: true,
				user_id: 'visual-user',
				username: 'operator',
				display_name: 'Operator',
				role: 'user'
			}
		})
	);
	await page.route('**/api/config', (route) =>
		route.fulfill({
			json: {
				auth_mode: 'password',
				needs_setup: false,
				signup_enabled: false,
				version: 'visual-test'
			}
		})
	);
	await page.route('**/api/git/config*', (route) =>
		route.fulfill({
			json: {
				root: '/workspace/project',
				git: { installed: false, is_repo: false },
				gh: { installed: false },
				permissions: { can_manage_gh: false, can_manage_commit_model: false }
			}
		})
	);
	await page.route('**/api/state/preferences', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/state/workspaces', (route) =>
		route.fulfill({ json: [{ path: '/workspace/project', name: 'Project', unread_count: 0 }] })
	);
	await page.route('**/v1/flowdeck/checkpoints*', (route) =>
		route.fulfill({ json: { checkpoints: [] } })
	);
});

test('FlowDeck diagnostics panel redacts unsafe containment details', async ({ page }) => {
	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) =>
		route.fulfill({
			json: {
				categories: [
					'process_failure',
					'workspace_lease',
					'future_category',
					'/srv/flowdeck/private/credentials.json'
				],
				diagnostics: [
					{
						id: 'diag-safe-process',
						run_id: 'run-safe-process',
						sequence: 7,
						category: 'process_failure',
						fallback: 'native',
						run_status: 'failed',
						run_outcome: 'native_fallback',
						created_at: 1_735_689_600_000,
						process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
						path: '/srv/flowdeck/private/credentials.json',
						credential: 'Bearer super-secret-token',
						exception: 'Error: private exception text'
					},
					{
						id: 'diag-safe-lease',
						run_id: 'run-safe-lease',
						sequence: 8,
						category: 'workspace_lease',
						fallback: 'native',
						run_status: 'recovering',
						run_outcome: 'native_fallback',
						created_at: 1_735_689_601_000,
						owner: 'another-operator',
						owner_run_id: 'run-owned-by-someone-else',
						detail: 'Error: private exception text'
					},
					{
						id: 'diag-future-category',
						run_id: 'run-future-category',
						sequence: 9,
						category: 'future_category',
						fallback: 'native',
						run_status: 'failed',
						run_outcome: 'native_fallback',
						created_at: 1_735_689_602_000
					},
					{
						id: 'diag-malformed',
						run_id: 'run-malformed',
						sequence: 'not-a-sequence',
						category: 'timeout',
						fallback: 'native',
						run_status: 'failed',
						run_outcome: 'native_fallback',
						created_at: 1_735_689_603_000
					}
				],
				total: 4,
				server_exception: 'Error: private exception text'
			}
		})
	);

	await page.goto('/flowdeck');
	const panel = page.getByTestId('fdx-diagnostics-panel');
	await expect(panel).toBeVisible({ timeout: 15_000 });
	await expect(panel.locator('.diagnostic-category', { hasText: 'Process Failure' })).toBeVisible();
	await expect(panel.locator('.diagnostic-category', { hasText: 'Workspace Lease' })).toBeVisible();
	await expect(panel.getByRole('option', { name: 'Future Category' })).toHaveCount(0);
	await expect(panel.locator('.diagnostic-row')).toHaveCount(2);

	const rows = panel.locator('.diagnostic-row');
	await expect(rows.nth(0)).toContainText('Run run-safe');
	await expect(rows.nth(0)).toContainText('Native fallback · Failed');
	await expect(rows.nth(1)).toContainText('Run run-safe');
	await expect(rows.nth(1)).toContainText('Native fallback · Recovering');

	const renderedDocument = await page.locator('body').textContent();
	const renderedMarkup = await page.content();
	for (const detail of forbiddenDetails) {
		expect(renderedDocument).not.toContain(detail);
		expect(renderedMarkup).not.toContain(detail);
	}
});

test('FlowDeck diagnostics outage stays safe and leaves the run composer usable', async ({
	page
}) => {
	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) =>
		route.fulfill({
			status: 503,
			contentType: 'application/json',
			body: JSON.stringify({
				detail: 'Error: private exception text',
				path: '/srv/flowdeck/private/credentials.json',
				process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
				credential: 'Bearer super-secret-token'
			})
		})
	);

	await page.goto('/flowdeck');
	const panel = page.getByTestId('fdx-diagnostics-panel');
	await expect(panel).toBeVisible({ timeout: 15_000 });
	await expect(
		panel.getByText('Containment diagnostics are temporarily unavailable.', { exact: true })
	).toBeVisible();
	await expect(panel.locator('.diagnostic-row')).toHaveCount(0);

	const renderedDocument = await page.locator('body').textContent();
	const renderedMarkup = await page.content();
	for (const detail of forbiddenDetails) {
		expect(renderedDocument).not.toContain(detail);
		expect(renderedMarkup).not.toContain(detail);
	}

	const objective = page.getByLabel('Objective');
	await expect(page.getByRole('heading', { name: /Give the work/ })).toBeVisible();
	await expect(objective).toBeVisible();
	await objective.fill('Keep the workspace ready after a diagnostics outage.');
	await expect(page.getByRole('button', { name: /Start run/ })).toBeEnabled();
});
