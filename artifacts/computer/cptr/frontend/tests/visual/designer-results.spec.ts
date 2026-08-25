import { expect, test } from '@playwright/test';

test('Designer result surface exposes the complete deterministic fixture', async ({ page }) => {
	await page.goto('/__visual-regression');
	const surface = page.getByTestId('designer-results-surface');

	await expect(surface).toBeVisible();
	await expect(surface.getByTestId('designer-results')).toBeVisible();
	await expect(surface.getByText('Cedar / Signal')).toBeVisible();
	await expect(surface.getByTestId('card-designer-variant-quiet-ledger')).toBeVisible();
	await expect(surface.getByText('Viewport confidence')).toBeVisible();
	await expect(surface.getByText('Screenshot comparison')).toBeVisible();
	await expect(surface.getByText('Native transcript linked')).toBeVisible();

	const overflow = await surface.evaluate((element) => element.scrollWidth > element.clientWidth);
	expect(overflow, 'Designer result surface is horizontally clipped').toBe(false);
});

test('Designer selection affordances remain keyboard and action reachable', async ({ page }) => {
	await page.goto('/__visual-regression');
	const surface = page.getByTestId('designer-results-surface');
	const variant = surface.getByTestId('card-designer-variant-quiet-ledger');

	await variant.getByTestId('button-select-variant-quiet-ledger').click();
	await expect(variant.getByText('Selected for mix')).toBeVisible();
	await surface.getByTestId('button-designer-evidence').click();
	await expect(surface.getByTestId('designer-evidence')).toContainText('home-reference.png');
});