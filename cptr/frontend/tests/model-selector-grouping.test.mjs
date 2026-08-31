import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../src/', import.meta.url);
const cptrRoot = new URL('../../', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');
const readCptr = (path) => readFile(new URL(path, cptrRoot), 'utf8');

test('chat models expose source and agent metadata needed for grouping', async () => {
	const store = await read('lib/stores/chat.ts');
	const backend = await readCptr('routers/chat.py');
	assert.match(store, /source_name\?:\s*string/);
	assert.match(store, /agent_id\?:\s*string/);
	assert.match(store, /profile_id\?:\s*string/);
	assert.match(backend, /"source_name":\s*conn\.get\("name"\)/);
});

test('model selector and settings share provider or agent grouping rules', async () => {
	const selector = await read('lib/components/common/ModelSelector.svelte');
	const settings = await read('lib/components/Admin/Models.svelte');
	const displayUtils = await read('lib/utils/model-display.ts');

	assert.match(displayUtils, /export function sourceLabel/);
	assert.match(displayUtils, /export function displayModelName/);
	assert.match(displayUtils, /export function modelSearchText/);
	assert.match(displayUtils, /export function groupModelsBySource/);
	assert.match(selector, /from '\$lib\/utils\/model-display'/);
	assert.match(settings, /from '\$lib\/utils\/model-display'/);
	assert.match(selector, /section:/);
	assert.match(settings, /filteredModelGroups/);
	assert.match(settings, /groupModelsBySource\(filteredModels\)/);
	assert.match(settings, /\{group\.label\}/);
	assert.match(settings, /displayModelName\(model\)/);
});

test('model picker keeps full names readable and gives mobile substantially more space', async () => {
	const selector = await read('lib/components/common/ModelSelector.svelte');
	const dropdown = await read('lib/components/DropdownMenu.svelte');

	assert.match(selector, /wrapLabel:\s*true/);
	assert.match(selector, /min\(24rem,calc\(100vw-1rem\)\)/);
	assert.match(selector, /min\(58dvh,30rem\)/);
	assert.match(dropdown, /section\?:\s*string/);
	assert.match(dropdown, /wrapLabel\?:\s*boolean/);
	assert.match(dropdown, /whitespace-normal/);
	assert.match(dropdown, /break-words/);
});
