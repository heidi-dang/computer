import { expect, test } from '@playwright/test';

const fixturePath = '/__flowdeck-report-regression';
const runId = 'report-reload-fixture';
const workspace = '/workspace/project';

const unknownEvidenceSummary = {
	run_id: runId,
	owner: 'visual-user',
	entries: [
		{
			id: 'report-export-unknown',
			run_id: runId,
			owner: 'visual-user',
			sequence: 1,
			kind: 'EVIDENCE_REPORT_EXPORTED',
			authority: 'advisory',
			source: 'lifecycle',
			payload: {
				outcome: 'response_delivery_unknown',
				operation: 'evidence_report_export'
			},
			created_at: 1_800_000_000_000
		}
	],
	total: 1,
	truncated: false
};

test.beforeEach(async ({ page }) => {
	await page.route(`**/v1/flowdeck/orchestrations/${runId}*`, (route) =>
		route.fulfill({
			json: {
				run_id: runId,
				workspace,
				status: 'succeeded',
				objective: 'Verify an interrupted report can be recovered safely.',
				events: [],
				evidence_summary: unknownEvidenceSummary
			}
		})
	);
});

test('FlowDeck keeps an interrupted report retryable after reloading the run', async ({ page }) => {
	await page.goto(fixturePath);
	const status = page.getByTestId('flowdeck-export-status');
	const retryButton = page.getByTestId('button-flowdeck-export-evidence');

	await expect(status).toContainText('Delivery unknown');
	await expect(status).toContainText(
		'The server cannot verify whether the browser received the report. Retry this same report safely.'
	);
	await expect(retryButton).toBeVisible();

	await page.reload();

	await expect(status).toContainText('Delivery unknown');
	await expect(status).toContainText('Retry this same report safely');
	await expect(retryButton).toBeVisible();

	const requestKeys: string[] = [];
	await page.route(`**/v1/flowdeck/orchestrations/${runId}/evidence-report*`, async (route) => {
		requestKeys.push(route.request().headers()['idempotency-key'] || '');
		if (requestKeys.length === 1) {
			await route.abort('failed');
			return;
		}
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			headers: {
				'Content-Disposition': 'attachment; filename="flowdeck-evidence-report-reload.json"',
				'X-FlowDeck-Export-Outcome': 'retry'
			},
			body: JSON.stringify(unknownEvidenceSummary)
		});
	});

	await retryButton.click();
	await expect(status).toContainText('Delivery unknown');
	await expect(retryButton).toBeVisible();

	await retryButton.click();
	await expect(status).toContainText('Safe retry');
	await expect(status).toContainText('Browser receipt is not confirmed.');
	await expect(status).not.toContainText('Browser received');
	expect(requestKeys).toHaveLength(2);
	expect(requestKeys[0]).toMatch(/^[0-9a-f-]{36}$/);
	expect(requestKeys[1]).toBe(requestKeys[0]);
});
