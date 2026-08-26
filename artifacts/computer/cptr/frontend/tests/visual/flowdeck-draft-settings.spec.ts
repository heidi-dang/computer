import { expect, test } from '@playwright/test';

const fixturePath = '/__flowdeck-draft-settings-regression';
const retainedDraftKey = 'flowdeck:composer-draft:retained';
const ownedRunKey = 'flowdeck:owned-run';
const composerSessionKey = 'flowdeck:composer-draft';
const unrelatedLocalStorageKey = 'flowdeck:test-unrelated-preference';

const retainedDraft = {
	mode: 'composer',
	workspace: '/workspace/project',
	objective: 'Keep this objective private on the device.',
	expiresAt: 4_102_444_800_000
};
const ownedRun = {
	mode: 'run',
	runId: 'owned-run-must-survive',
	workspace: '/workspace/project',
	objective: 'An owned run must not be cleared.'
};
const composerSession = {
	mode: 'composer',
	workspace: '/workspace/project',
	objective: 'A session draft must not be cleared.'
};
const unrelatedLocalStorageValue = 'keep-this-preference';

test('General settings clears only the retained FlowDeck draft', async ({ page }) => {
	await page.goto(fixturePath);
	expect(page.url()).not.toContain('/flowdeck');

	const storageSection = page.locator('section[aria-labelledby="flowdeck-storage-heading"]');
	await page.evaluate(
		({
			retainedDraft,
			ownedRun,
			composerSession,
			unrelatedLocalStorageKey,
			unrelatedLocalStorageValue
		}) => {
			localStorage.setItem('flowdeck:composer-draft:retained', JSON.stringify(retainedDraft));
			localStorage.setItem(unrelatedLocalStorageKey, unrelatedLocalStorageValue);
			sessionStorage.setItem('flowdeck:owned-run', JSON.stringify(ownedRun));
			sessionStorage.setItem('flowdeck:composer-draft', JSON.stringify(composerSession));
		},
		{
			retainedDraft,
			ownedRun,
			composerSession,
			unrelatedLocalStorageKey,
			unrelatedLocalStorageValue
		}
	);
	await page.reload();

	await expect(storageSection).toContainText('Saved FlowDeck draft');
	await expect(storageSection).toContainText('A draft is saved on this device');
	await expect(
		storageSection.getByRole('button', { name: 'Clear saved FlowDeck draft' })
	).toBeEnabled();

	const beforeClear = await page.evaluate(
		({ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }) => ({
			retainedDraft: localStorage.getItem(retainedDraftKey),
			ownedRun: sessionStorage.getItem(ownedRunKey),
			composerSession: sessionStorage.getItem(composerSessionKey),
			unrelatedLocalStorage: localStorage.getItem(unrelatedLocalStorageKey)
		}),
		{ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }
	);
	expect(beforeClear.retainedDraft).toBe(JSON.stringify(retainedDraft));
	expect(beforeClear.ownedRun).toBe(JSON.stringify(ownedRun));
	expect(beforeClear.composerSession).toBe(JSON.stringify(composerSession));
	expect(beforeClear.unrelatedLocalStorage).toBe(unrelatedLocalStorageValue);

	await storageSection.getByRole('button', { name: 'Clear saved FlowDeck draft' }).click();

	await expect(storageSection).toContainText('No saved FlowDeck draft on this device.');
	await expect(
		storageSection.getByRole('button', { name: 'Clear saved FlowDeck draft' })
	).toBeDisabled();

	const afterClear = await page.evaluate(
		({ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }) => ({
			retainedDraft: localStorage.getItem(retainedDraftKey),
			ownedRun: sessionStorage.getItem(ownedRunKey),
			composerSession: sessionStorage.getItem(composerSessionKey),
			unrelatedLocalStorage: localStorage.getItem(unrelatedLocalStorageKey)
		}),
		{ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }
	);
	expect(afterClear.retainedDraft).toBeNull();
	expect(afterClear.ownedRun).toBe(beforeClear.ownedRun);
	expect(afterClear.composerSession).toBe(beforeClear.composerSession);
	expect(afterClear.unrelatedLocalStorage).toBe(beforeClear.unrelatedLocalStorage);

	await page.reload();
	await expect(storageSection).toContainText('No saved FlowDeck draft on this device.');
	await expect(
		storageSection.getByRole('button', { name: 'Clear saved FlowDeck draft' })
	).toBeDisabled();

	const afterReload = await page.evaluate(
		({ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }) => ({
			retainedDraft: localStorage.getItem(retainedDraftKey),
			ownedRun: sessionStorage.getItem(ownedRunKey),
			composerSession: sessionStorage.getItem(composerSessionKey),
			unrelatedLocalStorage: localStorage.getItem(unrelatedLocalStorageKey)
		}),
		{ retainedDraftKey, ownedRunKey, composerSessionKey, unrelatedLocalStorageKey }
	);
	expect(afterReload).toEqual({
		retainedDraft: null,
		ownedRun: beforeClear.ownedRun,
		composerSession: beforeClear.composerSession,
		unrelatedLocalStorage: beforeClear.unrelatedLocalStorage
	});
});
