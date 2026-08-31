import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('MCP traffic API exposes typed snapshot and cookie-authenticated SSE helpers', async () => {
	const api = await read('lib/apis/mcp.ts');

	assert.match(api, /export interface McpTrafficEvent/);
	assert.match(api, /export interface McpTrafficSnapshot/);
	assert.match(api, /getMcpTrafficSnapshot/);
	assert.match(api, /openMcpTrafficStream/);
	assert.match(api, /new EventSource\(['"]\/api\/mcp\/traffic\/stream['"]\)/);
	assert.doesNotMatch(api, /traffic\/stream[^\n]*(token|authorization|bearer)/i);
});

test('MCP traffic reducer handles request lifecycle idempotently', async () => {
	const store = await read('lib/stores/mcp-traffic.ts');

	assert.match(store, /export function hydrateMcpTraffic/);
	assert.match(store, /export function applyMcpTrafficEvent/);
	assert.match(store, /export function recentRequestRows/);
	assert.match(store, /request_started/);
	assert.match(store, /request_finished/);
	assert.match(store, /request_failed/);
	assert.match(store, /ingestion_sequence\s*<=\s*state\.sequence/);
	assert.match(store, /seenEventIds/);
});

test('topology projection uses stable client ordering and deterministic radial positions', async () => {
	const store = await read('lib/stores/mcp-traffic.ts');

	assert.match(store, /export function topologyNodes/);
	assert.match(store, /localeCompare/);
	assert.match(store, /-Math\.PI\s*\/\s*2/);
	assert.match(store, /2\s*\*\s*Math\.PI/);
	assert.doesNotMatch(store, /Math\.random/);
});
