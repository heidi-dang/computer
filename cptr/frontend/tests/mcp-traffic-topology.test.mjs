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

test('reducer projects request completion and failure without unsafe payload fields', () => {
	let state = reducer.hydrateMcpTraffic(snapshot());
	state = reducer.applyMcpTrafficEvent(state, trafficEvent(1, 'request_started'));
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(2, 'request_finished', {
			request_id: 'request-1',
			status: 'complete',
			duration_ms: 25,
			response_bytes: 42
		})
	);
	assert.equal(state.clients.chatgpt.activeRequests, 0);
	assert.equal(state.clients.chatgpt.totalRequests, 1);
	let row = reducer.recentRequestRows(state)[0];
	assert.equal(row.status, 'complete');
	assert.equal(row.durationMs, 25);
	assert.equal(row.responseBytes, 42);
	assert.equal('arguments' in row, false);
	assert.equal('result' in row, false);

	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(3, 'request_failed', {
			request_id: 'request-missing-start',
			status: 'error',
			duration_ms: 10,
			error_code: 'tool_error'
		})
	);
	assert.equal(state.clients.chatgpt.totalRequests, 2);
	assert.equal(state.clients.chatgpt.errors, 1);
	row = reducer.recentRequestRows(state)[0];
	assert.equal(row.status, 'error');
	assert.equal(row.errorCode, 'tool_error');
});

test('topology UI exposes live graph, safe request table, responsive layout and console switch', async () => {
	const [page, topology, graph, recent] = await Promise.all([
		read('routes/mcp/+page.svelte'),
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpTopologyGraph.svelte'),
		read('lib/components/mcp/McpRecentRequests.svelte')
	]);

	assert.match(page, /Topology/);
	assert.match(page, /Console/);
	assert.match(page, /McpTopology/);
	assert.match(page, /McpConsole/);
	assert.match(topology, /getMcpTrafficSnapshot/);
	assert.match(topology, /openMcpTrafficStream/);
	assert.match(topology, /reconnecting/);
	assert.match(topology, /1000,\s*2000,\s*4000,\s*8000/);
	assert.match(topology, /grid-cols-1/);
	assert.match(topology, /lg:grid-cols/);
	assert.match(graph, /<svg/);
	assert.match(graph, /CPTR MCP/);
	assert.match(graph, /traffic-particle/);
	assert.match(graph, /center-ripple/);
	assert.match(graph, /prefers-reduced-motion:\s*reduce/);
	assert.match(recent, /Client/);
	assert.match(recent, /Method \/ Tool/);
	assert.match(recent, /In \/ Out/);
	assert.match(recent, /Status/);
	assert.match(recent, /When/);
	assert.doesNotMatch(recent, /record\.arguments|record\.result|authorization|bearer token/i);
});

test('MCP Console uses one full-width pane at a time on mobile', async () => {
	const console = await read('lib/components/mcp/McpConsole.svelte');

	assert.match(console, /type MobileConsoleView = 'servers' \| 'console' \| 'tool'/);
	assert.match(console, /let mobileView = \$state<MobileConsoleView>\('servers'\)/);
	assert.match(console, /lg:hidden/);
	assert.match(console, />\s*Servers\s*</);
	assert.match(console, />\s*Activity\s*</);
	assert.match(console, />\s*Tool\s*</);
	assert.match(console, /flex-col[^"\n]*lg:flex-row/);
	assert.match(console, /mobileView === 'servers'/);
	assert.match(console, /mobileView === 'console'/);
	assert.match(console, /mobileView === 'tool'/);
	assert.match(console, /lg:w-56/);
	assert.match(console, /lg:w-72/);
	assert.match(console, /mobileView = 'tool'/);
	assert.match(console, /mobileView = 'console'/);
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

const activityEvent = (sequence, phase, overrides = {}) => ({
	version: 1,
	event_id: `activity-${String(sequence).padStart(3, '0')}`,
	sequence,
	ingestion_sequence: sequence,
	timestamp_ms: 1_788_000_100_000 + sequence,
	client: { id: 'chatgpt', label: 'ChatGPT', version: '1' },
	session_id: 'session-1',
	request_id: 'request-1',
	tool_name: 'cptr_list_workspaces',
	title: 'List workspaces',
	phase,
	summary: phase === 'started' ? 'Working: List workspaces.' : 'Completed: List workspaces.',
	arguments_json: phase === 'started' ? '{"include_unavailable":false}' : null,
	result_json: phase === 'complete' ? '{"workspaces":[]}' : null,
	error_json: phase === 'failed' ? '{"code":"mcp_tool_error"}' : null,
	duration_ms: phase === 'started' ? null : 25,
	...overrides
});

const activitySnapshot = (events = [], eventCapacity = 8) => ({
	version: 1,
	sequence: events.at(-1)?.ingestion_sequence ?? 0,
	events,
	stream_health: {
		subscriber_count: 0,
		slow_subscriber_drops: 0,
		event_capacity: eventCapacity,
		subscriber_queue_capacity: 8
	}
});

test('MCP Activity API and Console feed use snapshot/SSE with presentation-only clear', async () => {
	const [api, console, feed, card] = await Promise.all([
		read('lib/apis/mcp.ts'),
		read('lib/components/mcp/McpConsole.svelte'),
		read('lib/components/mcp/McpActivityFeed.svelte'),
		read('lib/components/mcp/McpCallCard.svelte')
	]);

	assert.match(api, /export interface McpActivityEvent/);
	assert.match(api, /export interface McpActivitySnapshot/);
	assert.match(api, /getMcpActivitySnapshot/);
	assert.match(api, /openMcpActivityStream/);
	assert.match(api, /new EventSource\(['"]\/api\/mcp\/activity\/stream['"]\)/);
	assert.match(console, /McpActivityFeed/);
	assert.match(console, /Console invocation/);
	assert.match(console, />\s*Servers\s*</);
	assert.match(console, />\s*Activity\s*</);
	assert.match(console, />\s*Tool\s*</);
	assert.match(feed, /hiddenBeforeSequence/);
	assert.match(feed, /onClearConsole/);
	assert.match(feed, /Refresh/);
	assert.match(feed, /1000,\s*2000,\s*4000,\s*8000/);
	assert.match(card, />\s*Input\s*</);
	assert.match(card, />\s*Output\s*</);
	assert.match(card, />\s*Error\s*</);
});

test('MCP Activity reducer folds started and complete into one bounded row with preserved input', async () => {
	const activityReducer = await import(
		new URL('../src/lib/stores/mcp-activity.ts', import.meta.url)
	);
	const state = activityReducer.hydrateMcpActivity(
		activitySnapshot([activityEvent(1, 'started'), activityEvent(2, 'complete')], 4)
	);
	assert.equal(state.rows.length, 1);
	const row = state.rows[0];
	assert.equal(row.phase, 'complete');
	assert.equal(row.clientLabel, 'ChatGPT');
	assert.equal(row.argumentsJson, '{"include_unavailable":false}');
	assert.equal(row.resultJson, '{"workspaces":[]}');
	assert.equal(row.errorJson, null);
	assert.equal(row.durationMs, 25);
	assert.equal(row.source, 'plugin');
});

test('MCP Activity reducer preserves started input for failed terminal event and ignores duplicates', async () => {
	const activityReducer = await import(
		new URL('../src/lib/stores/mcp-activity.ts', import.meta.url)
	);
	let state = activityReducer.hydrateMcpActivity(activitySnapshot([], 2));
	state = activityReducer.applyMcpActivityEvent(
		state,
		activityEvent(1, 'started', { request_id: 'request-failed' })
	);
	state = activityReducer.applyMcpActivityEvent(
		state,
		activityEvent(2, 'failed', {
			request_id: 'request-failed',
			summary: 'Failed: List workspaces.'
		})
	);
	const same = activityReducer.applyMcpActivityEvent(state, activityEvent(2, 'failed'));
	assert.equal(same, state);
	assert.equal(state.rows.length, 1);
	assert.equal(state.rows[0].phase, 'failed');
	assert.equal(state.rows[0].argumentsJson, '{"include_unavailable":false}');
	assert.equal(state.rows[0].errorJson, '{"code":"mcp_tool_error"}');
});

test('MCP Activity reducer merges a local Console invocation without forging plugin origin', async () => {
	const activityReducer = await import(
		new URL('../src/lib/stores/mcp-activity.ts', import.meta.url)
	);
	const state = activityReducer.hydrateMcpActivity(activitySnapshot([], 2));
	const local = activityReducer.mergeConsoleActivityRow(state, {
		id: 'console-1',
		correlationKey: 'console:console-1',
		source: 'console',
		sequence: 0,
		clientId: null,
		clientLabel: 'Console invocation',
		clientVersion: null,
		toolName: 'local_tool',
		title: 'local_tool',
		phase: 'started',
		summary: 'Console invocation',
		startedAt: 1,
		completedAt: null,
		durationMs: null,
		argumentsJson: '{}',
		resultJson: null,
		errorJson: null,
		requestId: null,
		sessionId: null
	});
	assert.equal(local.rows.length, 1);
	assert.equal(local.rows[0].source, 'console');
	assert.equal(local.rows[0].clientLabel, 'Console invocation');
});
