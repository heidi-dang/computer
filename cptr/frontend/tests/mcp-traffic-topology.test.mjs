import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');
const reducer = await import(new URL('../src/lib/stores/mcp-traffic.ts', import.meta.url));

const snapshot = (eventCapacity = 4, sessionCapacity = 2) => ({
	version: 1,
	sequence: 0,
	center: { id: 'cptr-mcp', label: 'CPTR MCP', status: 'online' },
	clients: [],
	sessions: [],
	events: [],
	stream_health: {
		subscriber_count: 0,
		slow_subscriber_drops: 0,
		session_evictions: 0,
		request_evictions: 0,
		expired_sessions: 0,
		event_capacity: eventCapacity,
		session_capacity: sessionCapacity
	}
});

const trafficEvent = (sequence, eventType, overrides = {}) => ({
	version: 1,
	event_id: `event-${String(sequence).padStart(3, '0')}`,
	sequence,
	ingestion_sequence: sequence,
	event_type: eventType,
	timestamp_ms: 1_788_000_000_000 + sequence,
	session_id: 'session-1',
	client: { id: 'chatgpt', label: 'ChatGPT', version: '1' },
	request_id: `request-${sequence}`,
	method: 'tools/call',
	tool_name: 'cptr_list_workspaces',
	status: eventType.endsWith('failed')
		? 'error'
		: eventType.endsWith('finished')
			? 'complete'
			: 'started',
	duration_ms: null,
	request_bytes: 10,
	response_bytes: null,
	error_code: null,
	...overrides
});

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

	const state = reducer.hydrateMcpTraffic({
		...snapshot(),
		clients: [
			{
				id: 'gemini',
				label: 'Gemini',
				version: null,
				active_sessions: 1,
				active_requests: 0,
				total_requests: 0,
				errors: 0,
				last_seen: 2,
				last_tool: null
			},
			{
				id: 'chatgpt',
				label: 'ChatGPT',
				version: null,
				active_sessions: 1,
				active_requests: 0,
				total_requests: 0,
				errors: 0,
				last_seen: 1,
				last_tool: null
			}
		]
	});
	const first = reducer.topologyNodes(state);
	const second = reducer.topologyNodes(state);
	assert.deepEqual(first, second);
	assert.deepEqual(
		first.map((node) => node.id),
		['chatgpt', 'gemini']
	);
});

test('reducer keeps active request and session maps bounded under missing terminal events', () => {
	let state = reducer.hydrateMcpTraffic(snapshot(2, 1));
	state = reducer.applyMcpTrafficEvent(state, trafficEvent(1, 'request_started'));
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(2, 'request_started', { request_id: 'request-2' })
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(3, 'request_started', { request_id: 'request-3' })
	);
	assert.deepEqual(Object.keys(state.activeRequests).sort(), ['request-2', 'request-3']);
	assert.equal(state.clients.chatgpt.activeRequests, 2);
	assert.equal(reducer.recentRequestRows(state).filter((row) => row.status === 'active').length, 2);

	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(4, 'session_opened', {
			request_id: null,
			session_id: 'session-a',
			status: 'connected'
		})
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(5, 'session_opened', {
			request_id: null,
			session_id: 'session-b',
			client: { id: 'gemini', label: 'Gemini', version: '1' },
			status: 'connected'
		})
	);
	assert.deepEqual(Object.keys(state.sessions), ['session-b']);
	assert.equal(state.clients.chatgpt.activeSessions, 0);
	assert.equal(state.clients.gemini.activeSessions, 1);
});

test('reducer ignores duplicate and stale ingestion sequences', () => {
	const initial = reducer.hydrateMcpTraffic(snapshot());
	const event = trafficEvent(1, 'request_started');
	const once = reducer.applyMcpTrafficEvent(initial, event);
	const duplicate = reducer.applyMcpTrafficEvent(once, event);
	const stale = reducer.applyMcpTrafficEvent(
		once,
		trafficEvent(2, 'request_started', { ingestion_sequence: 1, event_id: 'event-stale' })
	);
	assert.equal(duplicate, once);
	assert.equal(stale, once);
});
