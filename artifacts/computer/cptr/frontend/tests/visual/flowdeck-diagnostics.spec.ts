import { expect, test } from '@playwright/test';

const forbiddenDetails = [
	'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
	'/srv/flowdeck/private/credentials.json',
	'Bearer super-secret-token',
	'Error: private exception text',
	'Error: provider private stack',
	'/var/lib/flowdeck/runtime/private.env',
	'Bearer flowdeck-secret',
	'future_category',
	'another-operator',
	'run-owned-by-someone-else'
];

async function expectDocumentToStaySafe(page: import('@playwright/test').Page) {
	const renderedDocument = await page.locator('body').textContent();
	const renderedMarkup = await page.content();
	for (const detail of forbiddenDetails) {
		expect(renderedDocument).not.toContain(detail);
		expect(renderedMarkup).not.toContain(detail);
	}
}

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
	await page.route('**/api/state/preferences**', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/state/workspaces**', (route) =>
		route.fulfill({ json: [{ path: '/workspace/project', name: 'Project', unread_count: 0 }] })
	);
	await page.route('**/v1/flowdeck/checkpoints*', (route) =>
		route.fulfill({ json: { checkpoints: [] } })
	);
	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) =>
		route.fulfill({ json: { categories: [], diagnostics: [], total: 0 } })
	);
});

test('FlowDeck diagnostics category filters keep unsafe details redacted', async ({ page }) => {
	const diagnosticsResponse = {
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
	};

	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) => {
		const category = new URL(route.request().url()).searchParams.get('category');
		const diagnostics = category
			? diagnosticsResponse.diagnostics.filter((diagnostic) => diagnostic.category === category)
			: diagnosticsResponse.diagnostics;
		return route.fulfill({
			json: {
				...diagnosticsResponse,
				diagnostics,
				total: diagnostics.length
			}
		});
	});

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

	const filteredRequest = page.waitForRequest(
		(request) =>
			request.url().includes('/v1/flowdeck/diagnostics/fdx-containment') &&
			new URL(request.url()).searchParams.get('category') === 'process_failure'
	);
	await panel.locator('select').selectOption('process_failure');
	await filteredRequest;
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);
	await expect(panel.locator('.diagnostic-category')).toHaveText('Process Failure');
	await expect(panel.locator('.diagnostic-row')).toContainText('Run run-safe');
	await expect(panel.locator('.diagnostic-row')).toContainText('Native fallback · Failed');
	await expect(panel.getByRole('option', { name: 'Future Category' })).toHaveCount(0);

	await expectDocumentToStaySafe(page);
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
	await expect(panel.getByRole('button', { name: 'Retry diagnostics' })).toBeVisible();
	await expect(panel.locator('.diagnostic-row')).toHaveCount(0);

	await expectDocumentToStaySafe(page);

	const objective = page.getByLabel('Objective');
	await expect(page.getByRole('heading', { name: /Give the work/ })).toBeVisible();
	await expect(objective).toBeVisible();
	await objective.fill('Keep the workspace ready after a diagnostics outage.');
	await expect(page.getByRole('button', { name: /Start run/ })).toBeEnabled();
});

test('FlowDeck diagnostics retry recovers without changing the selected category', async ({
	page
}) => {
	const diagnosticsResponse = {
		categories: ['process_failure', 'workspace_lease'],
		diagnostics: [
			{
				id: 'diag-retry-process',
				run_id: 'run-retry-process',
				sequence: 3,
				category: 'process_failure',
				fallback: 'native',
				run_status: 'failed',
				run_outcome: 'native_fallback',
				created_at: 1_735_689_600_000
			}
		],
		total: 1
	};
	let diagnosticsRequestCount = 0;

	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) => {
		diagnosticsRequestCount += 1;
		if (diagnosticsRequestCount === 2) {
			return route.fulfill({
				status: 503,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'temporary diagnostics outage' })
			});
		}

		const category = new URL(route.request().url()).searchParams.get('category');
		const diagnostics = category
			? diagnosticsResponse.diagnostics.filter((diagnostic) => diagnostic.category === category)
			: diagnosticsResponse.diagnostics;
		return route.fulfill({
			json: {
				...diagnosticsResponse,
				diagnostics,
				total: diagnostics.length
			}
		});
	});

	await page.goto('/flowdeck');
	const panel = page.getByTestId('fdx-diagnostics-panel');
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);

	await panel.locator('select').selectOption('process_failure');
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);

	await expect(
		panel.getByText('Containment diagnostics are temporarily unavailable.', { exact: true })
	).toHaveCount(0);
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);
	await expect(panel.locator('.diagnostic-category')).toHaveText('Process Failure');
	expect(diagnosticsRequestCount).toBe(3);
});

test('FlowDeck diagnostics automatic retries stop at the configured bound', async ({ page }) => {
	let diagnosticsRequestCount = 0;

	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) => {
		diagnosticsRequestCount += 1;
		return route.fulfill({
			status: 503,
			contentType: 'application/json',
			body: JSON.stringify({
				detail: 'Error: private exception text',
				path: '/srv/flowdeck/private/credentials.json',
				process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER'
			})
		});
	});

	await page.goto('/flowdeck');
	const panel = page.getByTestId('fdx-diagnostics-panel');
	await expect(
		panel.getByText('Containment diagnostics are temporarily unavailable.', { exact: true })
	).toBeVisible();
	expect(diagnosticsRequestCount).toBe(4);

	await page.waitForTimeout(1_500);
	expect(diagnosticsRequestCount).toBe(4);
	await expect(panel.getByRole('button', { name: 'Retry diagnostics' })).toBeEnabled();
	await expect(panel.locator('.diagnostic-row')).toHaveCount(0);
	await expectDocumentToStaySafe(page);
});

test('FlowDeck diagnostics refresh periodically without overlapping and stops on unmount', async ({
	page
}) => {
	await page.clock.install();
	const diagnosticsResponse = {
		categories: ['process_failure', 'workspace_lease'],
		diagnostics: [
			{
				id: 'diag-periodic-process',
				run_id: 'run-periodic-process',
				sequence: 4,
				category: 'process_failure',
				fallback: 'native',
				run_status: 'failed',
				run_outcome: 'native_fallback',
				created_at: 1_735_689_600_000
			}
		],
		total: 1
	};
	let diagnosticsRequestCount = 0;
	let periodicRequestUrl = '';
	let releasePeriodicRequest = () => {};

	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', async (route) => {
		diagnosticsRequestCount += 1;
		const category = new URL(route.request().url()).searchParams.get('category');
		if (diagnosticsRequestCount === 3) {
			periodicRequestUrl = route.request().url();
			await new Promise<void>((resolve) => {
				releasePeriodicRequest = resolve;
			});
		}
		const diagnostics = category
			? diagnosticsResponse.diagnostics.filter((diagnostic) => diagnostic.category === category)
			: diagnosticsResponse.diagnostics;
		await route.fulfill({
			json: {
				...diagnosticsResponse,
				diagnostics,
				total: diagnostics.length
			}
		});
	});

	await page.goto('/flowdeck');
	const panel = page.getByTestId('fdx-diagnostics-panel');
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);

	await panel.locator('select').selectOption('process_failure');
	await expect(panel.locator('select')).toHaveValue('process_failure');
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);
	expect(diagnosticsRequestCount).toBe(2);

	await page.clock.fastForward(30_000);
	await expect.poll(() => diagnosticsRequestCount).toBe(3);
	expect(periodicRequestUrl).toContain('category=process_failure');

	await page.clock.fastForward(30_000);
	expect(diagnosticsRequestCount).toBe(3);

	releasePeriodicRequest();
	await expect(panel.locator('.diagnostic-row')).toHaveCount(1);
	await expect(panel.locator('select')).toHaveValue('process_failure');

	await page.goto('/__visual-regression');
	await page.clock.fastForward(60_000);
	expect(diagnosticsRequestCount).toBe(3);
});

test('FlowDeck diagnostics stay newest-first and preserve a category change in flight', async ({
page
}) => {
const diagnosticsResponse = {
categories: ['process_failure', 'workspace_lease'],
diagnostics: [
{
id: 'diag-new-process',
run_id: 'run-new-process',
sequence: 2,
category: 'process_failure',
fallback: 'native',
run_status: 'failed',
run_outcome: 'native_fallback',
created_at: 1_735_689_601_000
},
{
id: 'diag-old-lease',
run_id: 'run-old-lease',
sequence: 1,
category: 'workspace_lease',
fallback: 'native',
run_status: 'recovering',
run_outcome: 'native_fallback',
created_at: 1_735_689_600_000
}
],
total: 2,
has_more: false
};
let diagnosticsRequestCount = 0;
let diagnosticsRequestUrls: string[] = [];
let releaseProcessRequest = () => {};

await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', async (route) => {
diagnosticsRequestCount += 1;
diagnosticsRequestUrls = [...diagnosticsRequestUrls, route.request().url()];
const category = new URL(route.request().url()).searchParams.get('category');
if (diagnosticsRequestCount === 2) {
await new Promise<void>((resolve) => {
releaseProcessRequest = resolve;
});
}
const diagnostics = category
? diagnosticsResponse.diagnostics.filter((diagnostic) => diagnostic.category === category)
: diagnosticsResponse.diagnostics;
await route.fulfill({
json: {
...diagnosticsResponse,
diagnostics,
total: diagnostics.length
}
});
});

await page.goto('/flowdeck');
const panel = page.getByTestId('fdx-diagnostics-panel');
await expect(panel.locator('.diagnostic-row').first()).toContainText('Run run-new-');
expect(diagnosticsRequestUrls[0]).toContain('limit=50');

await panel.locator('select').selectOption('process_failure');
await expect.poll(() => diagnosticsRequestCount).toBe(2);
await panel.locator('select').selectOption('workspace_lease');
expect(diagnosticsRequestCount).toBe(2);
releaseProcessRequest();

await expect.poll(() => diagnosticsRequestCount).toBe(3);
await expect(panel.locator('select')).toHaveValue('workspace_lease');
await expect(panel.locator('.diagnostic-category')).toHaveText('Workspace Lease');
await expect(panel.locator('.diagnostic-row')).toHaveCount(1);
});

test('FlowDeck orchestration errors use bounded copy', async ({ page }) => {
	await page.route('**/v1/flowdeck/orchestrations', (route) =>
		route.fulfill({
			status: 502,
			contentType: 'application/json',
			body: JSON.stringify({
				detail: 'Error: provider private stack',
				path: '/var/lib/flowdeck/runtime/private.env',
				process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
				authorization: 'Bearer flowdeck-secret'
			})
		})
	);

	await page.goto('/flowdeck');
	await page.getByLabel('Objective').fill('Coordinate a safe failure test.');
	await page.getByRole('button', { name: /Start run/ }).click();
	await expect(
		page.getByText('Controlled orchestration could not be started. Nothing was launched.', {
			exact: true
		})
	).toBeVisible();
	await expectDocumentToStaySafe(page);
});

test('FlowDeck session expiry renders login in place without a reload loop', async ({ page }) => {
	let authRequestCount = 0;
	await page.unroute('**/api/auth');
	await page.route('**/api/auth', (route) => {
		authRequestCount += 1;
		return route.fulfill({
			json: {
				authenticated: true,
				user_id: 'visual-user',
				username: 'operator',
				display_name: 'Operator',
				role: 'user'
			}
		});
	});
	await page.route('**/v1/flowdeck/diagnostics/fdx-containment*', (route) =>
		route.fulfill({ json: { categories: [], diagnostics: [], total: 0 } })
	);
	await page.route('**/v1/flowdeck/orchestrations', (route) =>
		route.fulfill({
			status: 401,
			contentType: 'application/json',
			body: JSON.stringify({
				detail: 'Error: expired-session private exception',
				path: '/srv/flowdeck/private/credentials.json',
				process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
				authorization: 'Bearer expired-session-secret'
			})
		})
	);

	await page.goto('/flowdeck');
	await expect(page.getByRole('heading', { name: /Give the work/ })).toBeVisible();
	await page.getByLabel('Objective').fill('Coordinate a safe session expiry transition.');
	await page.getByRole('button', { name: /Start run/ }).click();

	await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
	await expect(page.getByPlaceholder('Username')).toBeVisible();
	await expect(page).toHaveURL(/\/flowdeck$/);
	await page.waitForTimeout(500);
	expect(authRequestCount).toBe(1);
	await expectDocumentToStaySafe(page);
});

test('FlowDeck returns to the workspace after re-login without a reload', async ({ page }) => {
	let sessionAuthenticated = true;
	let orchestrationRequestCount = 0;
	await page.unroute('**/api/auth');
	await page.route('**/api/auth', (route) =>
		route.fulfill({
			json: sessionAuthenticated
				? {
						authenticated: true,
						user_id: 'visual-user',
						username: 'operator',
						display_name: 'Operator',
						role: 'user'
					}
				: { authenticated: false }
		})
	);
	await page.route('**/api/auth/login', (route) => {
		sessionAuthenticated = true;
		return route.fulfill({ json: { ok: true } });
	});
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
	await page.route('**/v1/flowdeck/orchestrations', (route) =>
		(async () => {
			orchestrationRequestCount += 1;
			sessionAuthenticated = false;
			return route.fulfill({
				status: 401,
				contentType: 'application/json',
				body: JSON.stringify({
					detail: 'Error: expired-session private exception',
					path: '/srv/flowdeck/private/credentials.json',
					process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
					authorization: 'Bearer expired-session-secret'
				})
			});
		})()
	);

	let navigationCount = 0;
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame()) navigationCount += 1;
	});
	await page.goto('/flowdeck');
	await expect(page.getByRole('heading', { name: /Give the work/ })).toBeVisible();
	const initialNavigationCount = navigationCount;
	await page.getByLabel('Objective').fill('Return to the existing workspace after re-login.');
	await page.getByRole('button', { name: /Start run/ }).click();

	await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
	const persistedDraft = await page.evaluate(() => {
		const raw = sessionStorage.getItem('flowdeck:composer-draft');
		return raw ? JSON.parse(raw) : null;
	});
	expect(persistedDraft).toEqual({
		mode: 'composer',
		workspace: '/workspace/project',
		objective: 'Return to the existing workspace after re-login.'
	});
	await page.getByPlaceholder('Username').fill('operator');
	await page.getByPlaceholder('Password').fill('valid-password');
	await page.getByRole('button', { name: /Sign In/ }).click();

	await expect(page.getByRole('heading', { name: /Give the work/ })).toBeVisible();
	await expect(page.getByLabel('Objective')).toBeVisible();
	await expect(page.getByLabel('Objective')).toHaveValue(
		'Return to the existing workspace after re-login.'
	);
	await expect(page.getByLabel('Workspace', { exact: true })).toHaveValue('/workspace/project');
	await expect(page.locator('#main-col')).toBeVisible();
	expect(orchestrationRequestCount).toBe(1);
	await expect
		.poll(() => navigationCount)
		.toBe(initialNavigationCount);
	const renderedDocument = await page.locator('body').textContent();
	expect(renderedDocument).not.toContain('expired-session');
	expect(renderedDocument).not.toContain('backend response');
	await expectDocumentToStaySafe(page);
});

test('FlowDeck cancellation errors use bounded copy', async ({ page }) => {
	await page.route('**/v1/flowdeck/orchestrations', (route) =>
		route.fulfill({
			json: {
				run_id: 'run-cancel',
				status: 'running',
				workspace: '/workspace/project'
			}
		})
	);
	await page.route('**/v1/flowdeck/orchestrations/run-cancel/cancel*', (route) =>
		route.fulfill({
			status: 409,
			contentType: 'application/json',
			body: JSON.stringify({
				detail: 'Error: provider private stack',
				path: '/var/lib/flowdeck/runtime/private.env',
				process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
				authorization: 'Bearer flowdeck-secret'
			})
		})
	);

	await page.goto('/flowdeck');
	await page.getByLabel('Objective').fill('Coordinate a safe cancellation test.');
	await page.getByRole('button', { name: /Start run/ }).click();
	await expect(page.getByRole('heading', { name: 'Running' }).first()).toBeVisible();
	await page.getByRole('button', { name: /^Cancel$/ }).click();
	const cancellationRequest = page.waitForRequest((request) =>
		request.url().includes('/v1/flowdeck/orchestrations/run-cancel/cancel')
	);
	await page.getByRole('button', { name: 'Confirm' }).click();
	await cancellationRequest;
	await expect(page.getByRole('alert')).toContainText(
		'The cancellation request was not accepted. Try again shortly.'
	);
	await expectDocumentToStaySafe(page);
});

test('FlowDeck run state, activity, and evidence stay safe with malformed backend fields', async ({
	page
}) => {
	await page.route('**/v1/flowdeck/orchestrations/run-sensitive*', (route) =>
		route.fulfill({
			json: {
				run_id: 'run-sensitive',
				workspace: '/var/lib/flowdeck/runtime/private.env',
				objective: 'Error: provider private stack',
				status: 'failed',
				message: 'Bearer flowdeck-secret',
				plan: {
					steps: ['Review the bounded run'],
					process_output: 'PROCESS_OUTPUT_SHOULD_NOT_RENDER',
					path: '/var/lib/flowdeck/runtime/private.env'
				},
				scopes: {
					workspace: 'read-only',
					authorization: 'Bearer flowdeck-secret'
				},
				events: [
					{
						title: 'Run started',
						detail: 'Error: provider private stack',
						owner: 'another-operator',
						owner_run_id: 'run-owned-by-someone-else',
						created_at: 'not-a-timestamp'
					}
				],
				evidence: [
					{
						kind: 'verification',
						path: '/var/lib/flowdeck/runtime/private.env',
						exception: 'Error: private exception text'
					}
				]
			}
		})
	);

	await page.goto('/flowdeck?run_id=run-sensitive&workspace=%2Fworkspace%2Fproject');
	await expect(page.getByRole('heading', { name: 'Failed' }).first()).toBeVisible();
	await expect(page.getByText('Run started', { exact: true })).toBeVisible();
	await expect(page.getByText('Observed activity', { exact: true })).toBeVisible();
	await expectDocumentToStaySafe(page);
});
