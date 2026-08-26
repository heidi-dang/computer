import { expect, test, type Page } from '@playwright/test';

const fixturePath = '/__recovery-regression';
const forbiddenDetails = [
	'Error: runtime private exception',
	'Error: database private exception',
	'Error: generated-auth private exception',
	'Error: checkpoint private exception',
	'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
	'/srv/flowdeck/private/credentials.json',
	'/var/lib/flowdeck/runtime/private.env',
	'Bearer recovery-panel-secret',
	'password=recovery-panel-secret',
	'sk-live-recovery-panel-secret'
];

async function expectDocumentToStaySafe(page: Page) {
	const renderedDocument = await page.locator('body').textContent();
	const renderedMarkup = await page.content();

	for (const detail of forbiddenDetails) {
		expect(renderedDocument).not.toContain(detail);
		expect(renderedMarkup).not.toContain(detail);
	}
}

function hostileFailureBody(exception: string) {
	return JSON.stringify({
		detail: exception,
		path: '/srv/flowdeck/private/credentials.json',
		process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
		authorization: 'Bearer recovery-panel-secret',
		credential: 'password=recovery-panel-secret',
		api_key: 'sk-live-recovery-panel-secret'
	});
}

function failWithHostileResponse(route: import('@playwright/test').Route, exception: string) {
	return route.fulfill({
		status: 502,
		contentType: 'application/json',
		body: hostileFailureBody(exception)
	});
}

test.beforeEach(async ({ page }) => {
	await page.route(/\/v1\/flowdeck\/checkpoints(?:\?.*)?$/, (route) =>
		route.fulfill({ json: { checkpoints: [] } })
	);
});

test('managed runtime start, poll, and stop failures stay bounded', async ({ page }) => {
	await page.route('**/v1/flowdeck/runtime/start', (route) =>
		failWithHostileResponse(route, 'Error: runtime private exception')
	);
	await page.goto(fixturePath);

	const panel = page.getByTestId('managed-runtime-panel');
	await panel.getByRole('button', { name: 'Start preview' }).click();
	await expect(panel).toHaveText(/Unable to start preview\. Try again shortly\./);
	await expectDocumentToStaySafe(page);

	await page.unroute('**/v1/flowdeck/runtime/start');
	await page.route('**/v1/flowdeck/runtime/start', (route) =>
		route.fulfill({
			json: { run_id: 'runtime-recovery-fixture', state: 'running', health: 'unknown' }
		})
	);
	let pollFailure = true;
	await page.route('**/v1/flowdeck/runtime/runtime-recovery-fixture*', (route) =>
		pollFailure
			? failWithHostileResponse(route, 'Error: runtime private exception')
			: route.fulfill({
					json: { run_id: 'runtime-recovery-fixture', state: 'running', health: 'unknown' }
				})
	);

	await panel.getByRole('button', { name: 'Start preview' }).click();
	await expect(panel).toHaveText(/Preview state unavailable\. Try again shortly\./);
	await expectDocumentToStaySafe(page);

	pollFailure = false;
	await page.waitForTimeout(600);
	await page.route('**/v1/flowdeck/runtime/runtime-recovery-fixture/stop*', (route) =>
		failWithHostileResponse(route, 'Error: runtime private exception')
	);
	await panel.getByRole('button', { name: 'Stop' }).click();
	await expect(panel).toHaveText(/Unable to stop preview\. Try again shortly\./);
	await expectDocumentToStaySafe(page);
});

test('project database inspect and query failures stay bounded', async ({ page }) => {
	await page.route('**/v1/flowdeck/database/inspect', (route) =>
		failWithHostileResponse(route, 'Error: database private exception')
	);
	await page.route('**/v1/flowdeck/database/query', (route) =>
		failWithHostileResponse(route, 'Error: database private exception')
	);
	await page.goto(fixturePath);

	const panel = page.getByTestId('project-database-panel');
	await panel.getByRole('button', { name: 'Inspect schema' }).click();
	await expect(panel).toHaveText(/Database inspection failed\. Review the request and try again\./);
	await expectDocumentToStaySafe(page);

	await panel.getByRole('button', { name: 'Run query' }).click();
	await expect(panel).toHaveText(/Database query failed\. Review the request and try again\./);
	await expectDocumentToStaySafe(page);
});

test('generated auth inspection failure stays bounded', async ({ page }) => {
	await page.route('**/v1/flowdeck/generated-auth/config*', (route) =>
		failWithHostileResponse(route, 'Error: generated-auth private exception')
	);
	await page.goto(fixturePath);

	const panel = page.getByTestId('generated-auth-panel');
	await panel.getByRole('button', { name: 'Inspect' }).click();
	await expect(panel).toHaveText(
		/Auth inspection failed\. Review the generated app configuration and try again\./
	);
	await expectDocumentToStaySafe(page);
});

test('generated auth sign-in and sign-up failures stay bounded', async ({ page }) => {
	await page.route('**/v1/flowdeck/generated-auth/config*', (route) =>
		route.fulfill({
			json: {
				provider: 'bounded-local',
				supported: true,
				verified: true,
				preserved_existing_auth: false,
				capabilities: { signup: true, external_callback: false }
			}
		})
	);
	await page.route('**/v1/flowdeck/generated-auth/csrf*', (route) =>
		route.fulfill({ json: { csrf: 'fixture-csrf' } })
	);
	await page.route('**/v1/flowdeck/generated-auth/session*', (route) =>
		route.fulfill({
			json: {
				user: { email: 'operator@example.test', role: 'operator' },
				expires_at: 1_800_000_000_000
			}
		})
	);
	await page.route('**/v1/flowdeck/generated-auth/signin', (route) =>
		failWithHostileResponse(route, 'Error: generated-auth private exception')
	);
	await page.route('**/v1/flowdeck/generated-auth/signup', (route) =>
		failWithHostileResponse(route, 'Error: generated-auth private exception')
	);
	await page.goto(fixturePath);

	const panel = page.getByTestId('generated-auth-panel');
	await panel.getByRole('button', { name: 'Inspect' }).click();
	await expect(panel.getByText('bounded-local', { exact: true })).toBeVisible();

	await panel.getByLabel('Generated app email').fill('operator@example.test');
	await panel.getByLabel('Generated app password').fill('fixture-password');
	await panel.getByRole('button', { name: 'Sign in' }).click();
	await expect(panel).toHaveText(/Sign in failed\. Check the submitted details and try again\./);
	await expectDocumentToStaySafe(page);

	await panel.getByRole('button', { name: 'Sign up' }).click();
	await expect(panel).toHaveText(/Sign up failed\. Check the submitted details and try again\./);
	await expectDocumentToStaySafe(page);
});

test('generated auth sign-out failure stays bounded', async ({ page }) => {
	await page.route('**/v1/flowdeck/generated-auth/config*', (route) =>
		route.fulfill({
			json: {
				provider: 'bounded-local',
				supported: true,
				verified: true,
				preserved_existing_auth: false,
				capabilities: { signup: true, external_callback: false }
			}
		})
	);
	await page.route('**/v1/flowdeck/generated-auth/csrf*', (route) =>
		route.fulfill({ json: { csrf: 'fixture-csrf' } })
	);
	await page.route('**/v1/flowdeck/generated-auth/session*', (route) =>
		route.fulfill({
			json: {
				user: { email: 'operator@example.test', role: 'operator' },
				expires_at: 1_800_000_000_000
			}
		})
	);
	await page.route('**/v1/flowdeck/generated-auth/signout', (route) =>
		failWithHostileResponse(route, 'Error: generated-auth private exception')
	);
	await page.goto(fixturePath);

	const panel = page.getByTestId('generated-auth-panel');
	await panel.getByRole('button', { name: 'Inspect' }).click();
	await expect(panel.getByRole('button', { name: 'Sign out' })).toBeVisible();
	await panel.getByRole('button', { name: 'Sign out' }).click();
	await expect(panel).toHaveText(/Sign out failed\. Try again shortly\./);
	await expectDocumentToStaySafe(page);
});

test('checkpoint refresh, capture, and restore failures stay bounded', async ({ page }) => {
	let checkpointMode: 'refresh-failure' | 'capture-failure' | 'restore-failure' = 'refresh-failure';
	const checkpointsCollection = /\/v1\/flowdeck\/checkpoints(?:\?.*)?$/;
	await page.route(checkpointsCollection, (route) => {
		if (checkpointMode === 'refresh-failure') {
			return failWithHostileResponse(route, 'Error: checkpoint private exception');
		}

		return route.fulfill({
			json: {
				checkpoints: [
					{
						checkpoint_id: 'checkpoint-recovery-fixture',
						revision: '1234567890abcdef',
						status: 'verified',
						created_at: 1_800_000_000_000
					}
				]
			}
		});
	});
	await page.route('**/v1/flowdeck/checkpoints/capture', (route) =>
		failWithHostileResponse(route, 'Error: checkpoint private exception')
	);
	await page.route('**/v1/flowdeck/checkpoints/restore', (route) =>
		failWithHostileResponse(route, 'Error: checkpoint private exception')
	);
	await page.goto(fixturePath);

	const panel = page.locator('.checkpoint-panel');
	await expect(panel).toHaveText(/Checkpoint state is unavailable\. Try again shortly\./);
	await expectDocumentToStaySafe(page);

	checkpointMode = 'capture-failure';
	await panel.getByRole('button', { name: 'Capture checkpoint' }).click();
	await expect(panel).toHaveText(/Capture was not completed\. Review the worktree and try again\./);
	await expectDocumentToStaySafe(page);

	checkpointMode = 'restore-failure';
	await page.reload();
	await expect(panel.getByRole('button', { name: 'Restore' })).toBeEnabled();
	await panel.getByRole('button', { name: 'Restore' }).click();
	await expect(panel).toHaveText(/Restore requires review\. No changes were applied\./);
	await expectDocumentToStaySafe(page);
});