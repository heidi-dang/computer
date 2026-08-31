import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const srcRoot = new URL('../src/', import.meta.url);
const cptrRoot = new URL('../../', import.meta.url);
const read = (path) => readFile(new URL(path, srcRoot), 'utf8');
const readCptr = (path) => readFile(new URL(path, cptrRoot), 'utf8');

test('models admin API exposes connection display metadata and atomic bulk update', async () => {
	const api = await read('lib/apis/admin.ts');
	const backend = await readCptr('routers/admin.py');

	assert.match(api, /source_name\?:\s*string/);
	assert.match(api, /bulkUpdateModelConfig/);
	assert.match(backend, /"source_name":\s*conn\.get\("name"\)/);
	assert.match(backend, /@router\.put\("\/models\/bulk\/config"\)/);
});

test('models settings filters by model and source metadata', async () => {
	const models = await read('lib/components/Admin/Models.svelte');
	const displayUtils = await read('lib/utils/model-display.ts');

	assert.match(models, /modelSearch/);
	assert.match(models, /filteredModels/);
	assert.match(models, /modelSearchText/);
	assert.match(displayUtils, /model\.source_name/);
	assert.match(displayUtils, /model\.agent_id/);
	assert.match(displayUtils, /model\.profile_id/);
	assert.match(models, /models\.searchPlaceholder/);
});

test('bulk model controls act on the currently shown models', async () => {
	const models = await read('lib/components/Admin/Models.svelte');

	assert.match(models, /bulkSetVisibleModels/);
	assert.match(models, /filteredModels\.filter/);
	assert.match(models, /bulkUpdateModelConfig/);
	assert.match(models, /models\.enableShown/);
	assert.match(models, /models\.disableShown/);
	assert.match(models, /models\.showingCount/);
});
