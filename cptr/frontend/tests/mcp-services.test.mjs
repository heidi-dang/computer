import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { compile, preprocess } from 'svelte/compiler';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('Services tab is registered next to Memory and lazy-loaded on /mcp', async () => {
	const page = await read('routes/mcp/+page.svelte');
	assert.match(
		page,
		/'topology' \| 'capability' \| 'console' \| 'factory' \| 'memory' \| 'services'/
	);
	assert.match(page, /typeof import\('\$lib\/components\/mcp\/McpServices\.svelte'\)\.default/);
	assert.match(page, /servicesLoad \?\?= import\('\$lib\/components\/mcp\/McpServices\.svelte'\)/);
	assert.match(page, /view === 'services'/);
	assert.match(page, />\s*Services\s*</);
	assert.match(page, /<LazyMcpServices \/>/);
	assert.match(page, /Loading services…/);
	assert.match(page, /view === 'memory'/);
});

test('McpServices compiles through the Svelte TypeScript preprocess path', async () => {
	const source = await read('lib/components/mcp/McpServices.svelte');
	const processed = await preprocess(source, vitePreprocess(), {
		filename: 'McpServices.svelte'
	});
	assert.doesNotThrow(() =>
		compile(processed.code, {
			filename: 'McpServices.svelte',
			generate: 'client'
		})
	);
});

test('McpServices renders aggregate bands, probes, and maintain controls', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpServices.svelte'),
		read('lib/apis/mcp.ts')
	]);

	assert.match(component, /getMcpServicesSnapshot/);
	assert.match(component, /openMcpServicesStream/);
	assert.match(component, /startMcpServicesMaintain/);
	assert.match(component, /crypto\.randomUUID\(\)/);
	assert.match(component, /startMcpServicesMaintain\(serviceId, idempotencyKey\)/);
	assert.match(component, /getMcpServicesMaintainJob/);
	assert.match(component, /bandClass/);
	assert.match(component, /'healthy'/);
	assert.match(component, /'moderate'/);
	assert.match(component, /'unhealthy'/);
	assert.match(component, /Stabilize All/);
	assert.match(component, /Why this band/);
	assert.match(component, /jobRunning/);
	assert.match(component, /disabled=\{jobRunning\}/);
	assert.match(component, /error instanceof ApiError && error\.status === 404/);
	assert.match(component, /maintainJob = null/);
	assert.match(component, /interrupted by a backend restart/);
	assert.match(component, /streamStatus === 'live'/);
	assert.match(component, /reconnecting/);

	assert.match(api, /export type McpServiceBand/);
	assert.match(api, /export interface McpServicesSnapshot/);
	assert.match(api, /\/api\/mcp\/services\/snapshot/);
	assert.match(api, /\/api\/mcp\/services\/stream/);
	assert.match(api, /\/api\/mcp\/services\/maintain/);
});

test('Stabilize All exposes accessible deterministic progress and final system state', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpServices.svelte'),
		read('lib/apis/mcp.ts')
	]);
	assert.match(component, /Stabilize All/);
	assert.match(component, /role="status"/);
	assert.match(component, /aria-live="polite"/);
	assert.match(component, /<progress/);
	assert.match(component, /system_status/);
	assert.match(component, /pass_count/);
	assert.match(component, /worker_watchdog/);
	assert.match(api, /McpMaintenanceSystemStatus/);
	assert.match(api, /'STABLE'/);
	assert.match(api, /'DEGRADED'/);
	assert.match(api, /'ACTION_REQUIRED'/);
	assert.match(api, /'FAILED'/);
	assert.match(api, /system_status:/);
	assert.match(api, /pass_count:/);
});

test('Services observability uses one SSE and mobile-first progressive disclosure', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpServices.svelte'),
		read('lib/apis/mcp.ts')
	]);

	assert.match(api, /export interface McpServicesTelemetry/);
	assert.match(api, /onTelemetry:/);
	assert.match(api, /addEventListener\('telemetry'/);
	assert.match(component, /onTelemetry:/);
	assert.match(component, /Runtime overview/);
	assert.match(component, /Active MCP/);
	assert.match(component, /Request p95/);
	assert.match(component, /Commands/);
	assert.match(component, /Workers/);
	assert.match(component, /<details/);
	assert.match(component, /mobile-edge-padding/);
	assert.match(component, /mobile-safe-bottom/);
	assert.match(component, /touch-target/);
	assert.match(component, /content-visibility:\s*auto/);
	assert.match(component, /contain-intrinsic-size/);
	assert.doesNotMatch(component, /loadSnapshot\(\)\.then\(\(\) => connectStream\(\)\)/);
});

test('Action Trace Explorer reuses Services SSE and loads bounded detail on demand', async () => {
	const [component, api] = await Promise.all([
		read('lib/components/mcp/McpServices.svelte'),
		read('lib/apis/mcp.ts')
	]);

	assert.match(api, /export interface McpActionTraceSummary/);
	assert.match(api, /export interface McpActionTraceDetail/);
	assert.match(api, /onTraces:/);
	assert.match(api, /addEventListener\('traces'/);
	assert.match(api, /getMcpActionTrace/);
	assert.match(api, /\/api\/mcp\/services\/traces\//);
	const servicesStream =
		api.match(
			/export function openMcpServicesStream[\s\S]*?return \(\) => source\.close\(\);\n\}/
		)?.[0] ?? '';
	assert.equal((servicesStream.match(/new EventSource/g) ?? []).length, 1);
	assert.match(component, /onTraces:/);
	assert.match(component, /Action traces/);
	assert.match(component, /ChatGPT/);
	assert.match(component, /MCP/);
	assert.match(component, /Backend/);
	assert.match(component, /Cleanup/);
	assert.match(component, /getMcpActionTrace/);
	assert.match(component, /aria-expanded=/);
	assert.match(component, /aria-controls=/);
	assert.match(component, /trace-timeline/);
	assert.match(component, /touch-target/);
	assert.match(component, /content-visibility:\s*auto/);
});

test('bandClass maps healthy/moderate/unhealthy to distinct visual tokens', async () => {
	const component = await read('lib/components/mcp/McpServices.svelte');
	const match = component.match(/function bandClass\([\s\S]*?\n\t\}/);
	assert.ok(match, 'bandClass function present');
	const body = match[0];
	assert.match(body, /band === 'healthy'/);
	assert.match(body, /band === 'moderate'/);
	assert.match(body, /emerald/);
	assert.match(body, /amber/);
	assert.match(body, /rose/);
});
