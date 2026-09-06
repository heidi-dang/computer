import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { compile, preprocess } from 'svelte/compiler';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('McpMemory compiles through the Svelte TypeScript preprocess path', async () => {
	const source = await read('lib/components/mcp/McpMemory.svelte');
	const processed = await preprocess(source, vitePreprocess(), {
		filename: 'McpMemory.svelte'
	});
	assert.doesNotThrow(() =>
		compile(processed.code, {
			filename: 'McpMemory.svelte',
			generate: 'client'
		})
	);
});

test('workspace visibility comes only from the authenticated owner-scoped snapshot', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpMemory.svelte'),
		read('lib/apis/mcp.ts')
	]);
	assert.match(component, /snapshot\?\.workspaces/);
	assert.match(component, /workspace\.workspace_id/);
	assert.match(component, /workspace\.workspace_name/);
	assert.match(component, /getMcpMemorySnapshot\(workspaceId\)/);
	assert.match(api, /\/api\/mcp\/memory\/snapshot/);
	assert.doesNotMatch(api, /memory\/snapshot[^\n]*user_id/);
});

test('canonical memory nodes are rendered and selectable by memory identity', async () => {
	const component = await read('lib/components/mcp/McpMemory.svelte');
	assert.match(component, /node\.kind === 'memory'/);
	assert.match(component, /node\.memory_id/);
	assert.match(component, /node\.preview/);
	assert.match(component, /selectedNodeId/);
	assert.match(component, /source_layer/);
});

test('memory SSE reports live state and degrades to reconnecting without discarding the snapshot', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpMemory.svelte'),
		read('lib/apis/mcp.ts')
	]);
	assert.match(component, /openMcpMemoryStream/);
	assert.match(component, /streamStatus = 'live'/);
	assert.match(component, /streamStatus = 'reconnecting'/);
	assert.match(component, /Realtime memory stream reconnecting/);
	assert.match(component, /streamStatus = snapshot \? 'reconnecting' : 'loading'/);
	assert.match(api, /new EventSource\(`\/api\/mcp\/memory\/stream/);
	assert.match(api, /addEventListener\('snapshot'/);
	assert.match(api, /addEventListener\('memory_error'/);
});

test('conflicts and stale verification are visible as first-class memory health state', async () => {
	const component = await read('lib/components/mcp/McpMemory.svelte');
	assert.match(component, /<strong>Conflicts<\/strong>/);
	assert.match(component, /open_memory_conflicts/);
	assert.match(component, /stale_verification_nodes/);
	assert.match(component, /selectedNode\.verification_stale/);
	assert.match(component, /stale · reverify/);
});

test('timeline requests remain workspace scoped and preserve historical controls', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpMemory.svelte'),
		read('lib/apis/mcp.ts')
	]);
	assert.match(component, /getMcpMemoryTimeline\(timelineAt, selectedWorkspaceId \|\| null, 300\)/);
	assert.match(component, /timelineBounds/);
	assert.match(component, /loadTimeline/);
	assert.match(api, /\/api\/mcp\/memory\/timeline/);
	assert.match(api, /known_at_ms/);
	assert.match(api, /workspace_id/);
});

test('search filtering matches memory metadata and retains the owning scope node', async () => {
	const component = await read('lib/components/mcp/McpMemory.svelte');
	assert.match(component, /function filterNodes/);
	assert.match(component, /node\.label/);
	assert.match(component, /node\.path/);
	assert.match(component, /node\.heading/);
	assert.match(component, /node\.memory_id/);
	assert.match(component, /node\.preview/);
	assert.match(component, /node\.workspace_name/);
	assert.match(component, /edge\.kind !== 'belongs_to'/);
	assert.match(component, /scopeIds\.add\(edge\.target\)/);
});
