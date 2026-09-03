import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('Dark Factory API exposes server-authoritative realtime progress and activity events', async () => {
	const api = await read('lib/apis/mcp.ts');

	assert.match(api, /export interface McpFactoryProgress/);
	assert.match(api, /basis:\s*'server_state_machine'/);
	assert.match(api, /new EventSource\(`\/api\/mcp\/factory\/stream\?/);
	assert.match(api, /addEventListener\(['"]activity['"]/);
	assert.match(api, /addEventListener\(['"]progress['"]/);
	assert.doesNotMatch(api, /factory\/stream[^\n]*(token|authorization|bearer)/i);
});

test('Dark Factory dashboard renders authoritative progress and canonical lifecycle states', async () => {
	const component = await read('lib/components/mcp/McpDarkFactory.svelte');

	assert.match(component, /role="progressbar"/);
	assert.match(component, /aria-valuenow=\{progressPercent\}/);
	assert.match(component, /selected\.progress\.effective_state/);
	assert.match(component, /Server-authoritative/);
	assert.match(component, /ROOT_CAUSE_ANALYSIS/);
	assert.match(component, /TRUST_EVALUATION/);
	assert.doesNotMatch(component, /['"]RCA['"]/);
	assert.doesNotMatch(component, /['"]RESEARCHING['"]/);
	assert.match(component, /onActivity:/);
	assert.match(component, /onProgress:/);
});

test('MCP shell and Dark Factory dashboard provide mobile-first touch layout', async () => {
	const page = await read('routes/mcp/+page.svelte');
	const component = await read('lib/components/mcp/McpDarkFactory.svelte');

	assert.match(page, /flex-wrap items-center gap-2 sm:flex-nowrap/);
	assert.match(page, /order-3 flex w-full min-w-0 overflow-x-auto/);
	assert.match(page, /min-h-11 min-w-max flex-1/);
	assert.match(component, /@media \(max-width: 639px\)/);
	assert.match(component, /min-height:\s*2\.75rem/);
	assert.match(component, /width:\s*min\(78vw, 15rem\)/);
	assert.match(component, /scroll-snap-type:\s*x proximity/);
	assert.match(component, /overflow-wrap:\s*anywhere/);
});
