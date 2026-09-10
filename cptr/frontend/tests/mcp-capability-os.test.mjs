import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { compile, preprocess } from 'svelte/compiler';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('Capability OS publishes one typed selected-task SSE contract', async () => {
	const api = await read('lib/apis/mcp.ts');
	assert.match(api, /export interface McpCapabilityOsStreamCallbacks/);
	assert.match(api, /export function openMcpCapabilityOsStream/);
	assert.match(
		api,
		/new EventSource\(`\/api\/mcp\/capability-os\/stream\?\$\{params\.toString\(\)\}`\)/
	);
	assert.match(api, /addEventListener\('snapshot'/);
	assert.match(api, /addEventListener\('capability_os_error'/);
	assert.match(api, /callbacks\.onOpen\?\.\(\)/);
	assert.match(api, /callbacks\.onError\?\.\(/);
	assert.match(api, /return \(\) => source\.close\(\)/);

	const helper =
		api.match(
			/export function openMcpCapabilityOsStream[\s\S]*?return \(\) => source\.close\(\);\n\}/
		)?.[0] ?? '';
	assert.equal((helper.match(/new EventSource/g) ?? []).length, 1);
});

test('Capability OS component compiles and uses real stream lifecycle instead of snapshot polling', async () => {
	const source = await read('lib/components/mcp/McpCapabilityOs.svelte');
	const processed = await preprocess(source, vitePreprocess(), {
		filename: 'McpCapabilityOs.svelte'
	});
	assert.doesNotThrow(() =>
		compile(processed.code, {
			filename: 'McpCapabilityOs.svelte',
			generate: 'client'
		})
	);

	assert.match(source, /openMcpCapabilityOsStream/);
	assert.match(
		source,
		/streamStatus = \$state<'connecting' \| 'live' \| 'reconnecting' \| 'error'>/
	);
	assert.match(source, /onSnapshot:/);
	assert.match(source, /onOpen:/);
	assert.match(source, /onError:/);
	assert.match(source, /streamStatus = 'live'/);
	assert.match(source, /streamStatus = 'reconnecting'/);
	assert.doesNotMatch(source, /loadSnapshot\(selectedTaskId, true\)/);
});

test('Design A is spatial, compact, runtime-truthful, and contains no fake prompt affordance', async () => {
	const source = await read('lib/components/mcp/McpCapabilityOs.svelte');

	assert.match(source, /class="spatial-stage"/);
	assert.match(source, /class="task-core/);
	assert.match(source, /class="orbit-node/);
	assert.match(source, /class="signal-strip"/);
	assert.match(source, /class="capability-ring"/);
	assert.match(source, /class="runtime-node"/);
	assert.match(source, /class="release-ladder"/);
	assert.match(source, /<details/);
	assert.match(source, /aria-live="polite"/);
	assert.match(source, /data-live=\{views\.taskCausality\.activeRuns > 0\}/);
	assert.match(source, /data-live=\{views\.mcpFabric\.activeMounts > 0\}/);
	assert.match(source, /data-live=\{views\.authority\.activeLeases > 0\}/);
	assert.match(source, /prefers-reduced-motion:\s*reduce/);
	assert.match(source, /min-height:\s*44px/);

	assert.doesNotMatch(source, /<textarea/);
	assert.doesNotMatch(source, /Send to ChatGPT/i);
	assert.doesNotMatch(source, /placeholder=["'][^"']*prompt/i);
});

test('Capability OS renders real task-scoped ChatGPT action lifecycle from backend traces', async () => {
	const [source, api] = await Promise.all([
		read('lib/components/mcp/McpCapabilityOs.svelte'),
		read('lib/apis/mcp.ts')
	]);

	assert.match(api, /export interface McpCapabilityOsLiveAction/);
	assert.match(api, /recentActions:\s*McpCapabilityOsLiveAction\[\]/);
	assert.match(api, /actionSequence:\s*number/);
	assert.match(source, /class="live-action-rail"/);
	assert.match(source, /class="live-action-row/);
	assert.match(source, /activeActionCount/);
	assert.match(source, /recentActions/);
	assert.match(source, /action\.source === 'chatgpt'/);
	assert.match(source, />ChatGPT</);
	assert.match(source, /aria-live="polite"/);
	assert.match(source, /data-status=\{action\.status\}/);
	assert.doesNotMatch(source, /fake action|simulated action|demo action/i);
});

test('Capability OS keeps CPTR semantic theme authority and avoids a replacement palette', async () => {
	const source = await read('lib/components/mcp/McpCapabilityOs.svelte');
	for (const token of [
		'--app-bg',
		'--app-surface',
		'--app-surface-raised',
		'--app-accent',
		'--app-border',
		'--app-fg-muted'
	]) {
		assert.match(source, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
	}
	assert.match(source, /color-mix\(in oklab/);
});
