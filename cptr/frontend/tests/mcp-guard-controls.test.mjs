import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { compile, preprocess } from 'svelte/compiler';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('Guard Controls is a lazy-loaded real /mcp view', async () => {
	const page = await read('routes/mcp/+page.svelte');
	assert.match(page, /'guards'/);
	assert.match(page, /McpGuardControls\.svelte/);
	assert.match(page, /view === 'guards'/);
	assert.match(page, />\s*Guard Controls\s*</);
	assert.match(page, /<LazyMcpGuardControls \/>/);
	assert.match(page, /Loading guard controls…/);
});

test('Guard Controls compiles through Svelte TypeScript preprocessing', async () => {
	const source = await read('lib/components/mcp/McpGuardControls.svelte');
	const processed = await preprocess(source, vitePreprocess(), {
		filename: 'McpGuardControls.svelte'
	});
	assert.doesNotThrow(() =>
		compile(processed.code, {
			filename: 'McpGuardControls.svelte',
			generate: 'client'
		})
	);
});

test('Guard Controls is backed only by authenticated owner guard APIs', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpGuardControls.svelte'),
		read('lib/apis/mcp.ts')
	]);

	assert.match(api, /export interface McpGuardControl/);
	assert.match(api, /export interface McpGuardControls/);
	assert.match(api, /getMcpGuardControls/);
	assert.match(api, /updateMcpGuardControl/);
	assert.match(api, /resetMcpGuardControls/);
	assert.match(api, /\/api\/mcp\/guards/);
	assert.match(api, /method:\s*'PATCH'/);
	assert.match(api, /expected_version/);

	assert.match(component, /getMcpGuardControls/);
	assert.match(component, /updateMcpGuardControl/);
	assert.match(component, /resetMcpGuardControls/);
	assert.match(component, /guard\.mutable/);
	assert.match(component, /Locked invariants/);
	assert.match(component, /Approval controls/);
	assert.match(component, /host_capabilities\.local_root_grants/);
	assert.match(component, /GUARD_VERSION_CONFLICT|409/);
	assert.match(component, /aria-checked=/);
	assert.match(component, /role="switch"/);
	assert.match(component, /min-h-11/);
	assert.match(component, /Retry/);
	assert.match(component, /Reset defaults/);
	assert.doesNotMatch(component, /Bearer|CPTR_API_TOKEN|Authorization/);
});
