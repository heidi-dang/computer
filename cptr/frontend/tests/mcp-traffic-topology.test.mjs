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
	correlation_id: null,
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

test('MCP topology config and diagnostics API use typed cookie-authenticated helpers', async () => {
	const api = await read('lib/apis/mcp.ts');

	assert.match(api, /export interface McpTopologyConfig/);
	assert.match(api, /getMcpTopologyConfig/);
	assert.match(api, /updateMcpTopologyConfig/);
	assert.match(api, /export interface McpDiagnosticsSnapshot/);
	assert.match(api, /getMcpDiagnosticsSnapshot/);
	assert.match(api, /openMcpDiagnosticsStream/);
	assert.match(api, /export interface McpUsageDiagnostic/);
	assert.match(api, /onUsage/);
	assert.match(api, /addEventListener\(['"]usage['"]/);
	assert.match(api, /new EventSource\(['"]\/api\/mcp\/diagnostics\/stream['"]\)/);
	assert.doesNotMatch(api, /diagnostics\/stream[^\n]*(token|authorization|bearer)/i);
});

test('topology aliases preserve canonical ids while changing display labels', async () => {
	const topologyStoreSource = await read('lib/stores/mcp-topology.ts').catch(() => '');
	assert.match(topologyStoreSource, /export type McpTopologySelection/);
	assert.match(topologyStoreSource, /export function displayTopologyLabel/);

	const topologyStore = await import(
		new URL('../src/lib/stores/mcp-topology.ts', import.meta.url)
	).catch(() => ({}));
	assert.equal(typeof topologyStore.displayTopologyLabel, 'function');
	assert.equal(
		topologyStore.displayTopologyLabel('cptr-backend', 'CPTR Backend', {
			'cptr-backend': 'Workstation'
		}),
		'Workstation'
	);
	assert.equal(
		topologyStore.displayTopologyLabel('cptr-backend', 'CPTR Backend', {}),
		'CPTR Backend'
	);
});

test('diagnostics reducer hydrates bounded state and ignores stale incremental events', async () => {
	const diagnostics = await import(
		new URL('../src/lib/stores/mcp-diagnostics.ts', import.meta.url)
	).catch(() => ({}));
	assert.equal(typeof diagnostics.hydrateMcpDiagnostics, 'function');
	assert.equal(typeof diagnostics.applyMcpDiagnosticsEvent, 'function');

	const failure = (sequence, id) => ({
		kind: 'failure',
		version: 1,
		diagnostic_id: id,
		request_id: 'request-1',
		correlation_id: 'corr-1',
		session_id: 'session-1',
		client_id: 'chatgpt',
		method: 'tools/call',
		tool_name: 'cptr_list_workspaces',
		stage: 'cptr_backend',
		error_code: 'backend_unavailable',
		http_status: 503,
		retryable: true,
		started_at_ms: 100,
		completed_at_ms: 125,
		duration_ms: 25,
		request_bytes: 10,
		response_bytes: 20,
		summary: 'Backend unavailable.',
		ingestion_sequence: sequence
	});
	const system = (sequence, timestamp) => ({
		kind: 'system',
		version: 1,
		timestamp_ms: timestamp,
		cpu_usage_percent: 25,
		cpu_count: 8,
		load_avg: [0.5],
		memory_total_bytes: 1000,
		memory_available_bytes: 400,
		disk_total_bytes: 2000,
		disk_used_bytes: 1000,
		disk_free_bytes: 1000,
		disk_read_bytes_per_s: 10,
		disk_write_bytes_per_s: 20,
		disk_read_ops_per_s: 1,
		disk_write_ops_per_s: 2,
		network_rx_bytes_per_s: 30,
		network_tx_bytes_per_s: 40,
		uptime_seconds: 50,
		gpu_status: 'unavailable',
		gpus: [],
		cptr_process: null,
		processes: [],
		ingestion_sequence: sequence
	});
	const snapshot = {
		version: 1,
		sequence: 5,
		latency: {
			'cptr-mcp-cptr-backend': {
				metric_type: 'backend_api_rtt',
				latest_ms: 25,
				average_ms: 20,
				p50_ms: 20,
				p95_ms: 25,
				max_ms: 25,
				sample_count: 2,
				last_updated_ms: 125,
				latest_status: 'ok',
				health: 'healthy'
			}
		},
		failures: [failure(0, 'failure-001')],
		system: [system(0, 100)],
		stream_health: {
			subscriber_count: 0,
			slow_subscriber_drops: 0,
			latency_sample_capacity_per_edge: 120,
			failure_capacity: 2,
			system_sample_capacity: 2,
			subscriber_queue_capacity: 64
		}
	};
	let state = diagnostics.hydrateMcpDiagnostics(snapshot);
	assert.equal(state.sequence, 5);
	assert.equal(state.latency['cptr-mcp-cptr-backend'].latestMs, 25);
	assert.equal(state.failures.length, 1);
	assert.equal(state.system.length, 1);

	const stale = diagnostics.applyMcpDiagnosticsEvent(state, failure(5, 'failure-stale'));
	assert.equal(stale, state);
	state = diagnostics.applyMcpDiagnosticsEvent(state, failure(6, 'failure-002'));
	state = diagnostics.applyMcpDiagnosticsEvent(state, failure(7, 'failure-003'));
	assert.deepEqual(
		state.failures.map((item) => item.diagnosticId),
		['failure-002', 'failure-003']
	);
	state = diagnostics.applyMcpDiagnosticsEvent(state, system(8, 200));
	state = diagnostics.applyMcpDiagnosticsEvent(state, system(9, 300));
	assert.deepEqual(
		state.system.map((item) => item.timestampMs),
		[200, 300]
	);
	state = diagnostics.applyMcpDiagnosticsEvent(state, {
		kind: 'latency',
		version: 1,
		event_id: 'latency-001',
		timestamp_ms: 400,
		request_id: 'request-1',
		correlation_id: 'corr-1',
		edge_id: 'cptr-mcp-cptr-backend',
		metric_type: 'backend_api_rtt',
		duration_ms: 55,
		status: 'error',
		ingestion_sequence: 10
	});
	assert.equal(state.latency['cptr-mcp-cptr-backend'].latestMs, 55);
	assert.equal(state.latency['cptr-mcp-cptr-backend'].latestStatus, 'error');
});

test('usage diagnostics hydrate bounded model state and project 60-second token/cost buckets', async () => {
	const diagnostics = await import(
		new URL('../src/lib/stores/mcp-diagnostics.ts', import.meta.url)
	);
	assert.equal(typeof diagnostics.usageTimeline, 'function');
	assert.equal(typeof diagnostics.usageTotals, 'function');
	assert.equal(typeof diagnostics.currentUsageModel, 'function');

	const base = 1_788_000_500_000;
	const usage = (sequence, timestamp, inputTokens, outputTokens, cost, overrides = {}) => ({
		kind: 'usage',
		version: 1,
		event_id: `usage-${String(sequence).padStart(4, '0')}`,
		timestamp_ms: timestamp,
		request_id: `request-${sequence}`,
		correlation_id: `corr-${sequence}`,
		session_id: 'session-1',
		client_id: 'chatgpt',
		model_reported: 'GPT-5.6 Sol',
		model_canonical: 'gpt-5.6-sol',
		model_source: 'self_reported',
		tool_name: 'cptr_list_workspaces',
		input_tokens_estimated: inputTokens,
		output_tokens_estimated: outputTokens,
		cached_input_tokens_estimated: null,
		estimator_method: 'o200k_base:fallback',
		estimator_exact_for_model: false,
		status: 'complete',
		pricing_status: 'current',
		pricing_version: 'openai-2026-08-21-promo',
		pricing_verified_at: '2026-09-02',
		pricing_valid_through: '2026-11-21',
		pricing_source_label: 'OpenAI API model pricing',
		pricing_source_url: 'https://developers.openai.com/api/docs/models/compare',
		input_usd_per_million: '4.00',
		cached_input_usd_per_million: '0.40',
		output_usd_per_million: '20.00',
		input_cost_usd: '0.0004',
		cached_input_cost_usd: null,
		output_cost_usd: '0.0004',
		simulated_cost_usd: cost,
		ingestion_sequence: sequence,
		...overrides
	});
	const first = usage(5, base + 1_000, 100, 20, '0.0008');
	const snapshot = {
		version: 1,
		sequence: 5,
		latency: {},
		failures: [],
		system: [],
		usage: [first],
		current_model: first,
		usage_totals: {
			input_tokens_estimated: 100,
			output_tokens_estimated: 20,
			total_tokens_estimated: 120,
			simulated_cost_usd: '0.0008',
			priced_events: 1,
			stale_events: 0,
			unpriced_events: 0,
			by_model: {}
		},
		stream_health: {
			subscriber_count: 0,
			slow_subscriber_drops: 0,
			latency_sample_capacity_per_edge: 120,
			failure_capacity: 2,
			system_sample_capacity: 2,
			usage_capacity: 2,
			subscriber_queue_capacity: 64
		}
	};
	let state = diagnostics.hydrateMcpDiagnostics(snapshot);
	assert.equal(state.usage.length, 1);
	assert.equal(diagnostics.currentUsageModel(state).modelReported, 'GPT-5.6 Sol');
	assert.deepEqual(diagnostics.usageTotals(state), {
		inputTokensEstimated: 100,
		outputTokensEstimated: 20,
		totalTokensEstimated: 120,
		simulatedCostUsd: 0.0008,
		pricedEvents: 1,
		staleEvents: 0,
		unpricedEvents: 0
	});

	state = diagnostics.applyMcpDiagnosticsEvent(state, usage(6, base + 6_000, 200, 40, '0.0016'));
	assert.deepEqual(diagnostics.usageTotals(state), {
		inputTokensEstimated: 300,
		outputTokensEstimated: 60,
		totalTokensEstimated: 360,
		simulatedCostUsd: 0.0024000000000000002,
		pricedEvents: 2,
		staleEvents: 0,
		unpricedEvents: 0
	});
	const timeline = diagnostics.usageTimeline(state, base + 10_000, {
		windowMs: 10_000,
		bucketMs: 5_000
	});
	assert.deepEqual(
		timeline.map((bucket) => ({
			input: bucket.inputTokens,
			output: bucket.outputTokens,
			cost: bucket.simulatedCostUsd
		})),
		[
			{ input: 100, output: 20, cost: 0.0008 },
			{ input: 200, output: 40, cost: 0.0016 }
		]
	);

	state = diagnostics.applyMcpDiagnosticsEvent(
		state,
		usage(7, base + 9_000, 50, 10, null, {
			model_reported: null,
			model_canonical: null,
			model_source: 'unavailable',
			pricing_status: 'model_not_reported',
			input_usd_per_million: null,
			cached_input_usd_per_million: null,
			output_usd_per_million: null,
			input_cost_usd: null,
			output_cost_usd: null,
			simulated_cost_usd: null
		})
	);
	assert.equal(state.usage.length, 2);
	assert.equal(diagnostics.currentUsageModel(state).modelReported, null);
	assert.deepEqual(diagnostics.usageTotals(state), {
		inputTokensEstimated: 350,
		outputTokensEstimated: 70,
		totalTokensEstimated: 420,
		simulatedCostUsd: 0.0024000000000000002,
		pricedEvents: 2,
		staleEvents: 0,
		unpricedEvents: 1
	});
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

test('snapshot hydration does not resurrect a pruned generic ChatGPT connector from event history', () => {
	const enrichedClient = {
		id: 'chatgpt-session-session-1',
		label: 'ChatGPT · MCP topology identity',
		version: '1',
		session_name: 'MCP topology identity',
		model: 'GPT-5.6 Sol',
		workspace_id: 'workspace-123',
		workspace_name: 'Desktop'
	};
	const state = reducer.hydrateMcpTraffic({
		...snapshot(8, 2),
		sequence: 2,
		clients: [
			{
				...enrichedClient,
				active_sessions: 1,
				active_requests: 0,
				total_requests: 1,
				errors: 0,
				last_seen: 1_788_000_000_002,
				last_tool: 'cptr_open_live_workbench'
			}
		],
		sessions: [
			{
				session_id: 'session-1',
				client_id: enrichedClient.id,
				connected_at: 1_788_000_000_000,
				last_seen: 1_788_000_000_002
			}
		],
		events: [
			trafficEvent(1, 'request_started', { request_id: 'request-1' }),
			trafficEvent(2, 'request_finished', {
				request_id: 'request-1',
				status: 'complete',
				client: enrichedClient
			})
		]
	});

	assert.deepEqual(
		reducer.topologyNodes(state).map((node) => node.id),
		[enrichedClient.id]
	);
	assert.equal(state.clients.chatgpt, undefined);
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

test('recent requests are capped to the latest 10 rows', () => {
	let state = reducer.hydrateMcpTraffic(snapshot(40, 4));
	let sequence = 1;
	for (let index = 1; index <= 12; index += 1) {
		const requestId = `request-${index}`;
		state = reducer.applyMcpTrafficEvent(
			state,
			trafficEvent(sequence++, 'request_started', { request_id: requestId })
		);
		state = reducer.applyMcpTrafficEvent(
			state,
			trafficEvent(sequence++, 'request_finished', {
				request_id: requestId,
				status: 'complete'
			})
		);
	}
	const rows = reducer.recentRequestRows(state);
	assert.equal(rows.length, 10);
	assert.equal(rows[0].requestId, 'request-12');
	assert.equal(rows.at(-1).requestId, 'request-3');
});

test('topology client state preserves session model and workspace identity metadata', () => {
	const enrichedClient = {
		id: 'chatgpt-session-session-1',
		label: 'ChatGPT · MCP topology identity',
		version: '1',
		session_name: 'MCP topology identity',
		model: 'GPT-5.6 Sol',
		workspace_id: 'workspace-123',
		workspace_name: 'Desktop'
	};
	let state = reducer.hydrateMcpTraffic(snapshot(8, 2));
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(1, 'session_opened', {
			request_id: null,
			status: 'connected',
			client: enrichedClient
		})
	);
	const node = reducer.topologyNodes(state)[0];
	assert.equal(node.sessionName, 'MCP topology identity');
	assert.equal(node.model, 'GPT-5.6 Sol');
	assert.equal(node.workspaceId, 'workspace-123');
	assert.equal(node.workspaceName, 'Desktop');
});

test('request statistics distinguish success, failure, active work and rolling time buckets', () => {
	assert.equal(typeof reducer.requestOutcomeTotals, 'function');
	assert.equal(typeof reducer.requestTimeline, 'function');

	const base = 1_788_000_200_000;
	let state = reducer.hydrateMcpTraffic(snapshot(16, 4));
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(1, 'request_started', {
			timestamp_ms: base + 500,
			request_id: 'request-success'
		})
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(2, 'request_finished', {
			timestamp_ms: base + 2_000,
			request_id: 'request-success',
			status: 'complete'
		})
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(3, 'request_started', {
			timestamp_ms: base + 5_500,
			request_id: 'request-failed'
		})
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(4, 'request_failed', {
			timestamp_ms: base + 7_000,
			request_id: 'request-failed',
			status: 'error',
			error_code: 'tool_error'
		})
	);
	state = reducer.applyMcpTrafficEvent(
		state,
		trafficEvent(5, 'request_started', {
			timestamp_ms: base + 9_000,
			request_id: 'request-active'
		})
	);

	assert.deepEqual(reducer.requestOutcomeTotals(state), {
		total: 2,
		success: 1,
		failed: 1,
		active: 1
	});
	assert.deepEqual(
		reducer
			.requestTimeline(state, base + 10_000, { windowMs: 10_000, bucketMs: 5_000 })
			.map((bucket) => ({ success: bucket.success, failed: bucket.failed, total: bucket.total })),
		[
			{ success: 1, failed: 0, total: 1 },
			{ success: 0, failed: 1, total: 1 }
		]
	);
});

test('MCP topology renders a premium interactive live request chart', async () => {
	const [topology, chart] = await Promise.all([
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpRequestChart.svelte').catch(() => '')
	]);

	assert.match(topology, /McpRequestChart/);
	assert.match(chart, /Live request statistics/);
	assert.match(chart, /Successful/);
	assert.match(chart, /Failed/);
	assert.match(chart, /Active/);
	assert.match(chart, /Last 60 seconds/);
	assert.match(chart, /McpTimeSeriesChart/);
	assert.match(chart, /requestTimeline/);
	assert.match(chart, /axisPointer/);
	assert.match(chart, /dataZoom/);
	assert.match(chart, /media:/);
	assert.match(chart, /hideOverlap/);
	assert.doesNotMatch(chart, /<polyline|polylinePoints/);
});

test('MCP topology renders estimated model token usage and API-equivalent simulated cost analytics', async () => {
	const [topology, panel] = await Promise.all([
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpUsageCostPanel.svelte').catch(() => '')
	]);

	assert.match(topology, /McpUsageCostPanel/);
	assert.match(topology, /onUsage:\s*applyDiagnosticEvent/);
	for (const label of [
		'Model usage & simulated cost',
		'Estimated · MCP-visible tokens',
		'Current model',
		'Estimated input',
		'Estimated output',
		'Estimated total',
		'Simulated cost (USD)',
		'Avg simulated cost/request',
		'Pricing status',
		'Self-reported',
		'Model not reported',
		'Unpriced',
		'Stale pricing',
		'Last 60 seconds',
		'Input tokens',
		'Output tokens',
		'Pricing details',
		'Input rate',
		'Cached input rate',
		'Output rate'
	]) {
		assert.ok(panel.includes(label), `usage panel must show ${label}`);
	}
	assert.match(panel, /usageTimeline/);
	assert.match(panel, /usageTotals/);
	assert.match(panel, /currentUsageModel/);
	assert.match(panel, /pricingVersion/);
	assert.match(panel, /Not your ChatGPT bill/);
	assert.match(panel, /reasoning/);
	assert.match(panel, /cache usage/);
	assert.match(panel, /final-answer tokens/);
	assert.match(panel, /Long-context multiplier not inferable from MCP-visible tokens/);
	assert.ok(
		(panel.match(/<McpTimeSeriesChart/g) ?? []).length >= 2,
		'usage panel must render interactive token and cost charts'
	);
	assert.match(panel, /axisPointer/);
	assert.match(panel, /dataZoom/);
	assert.match(panel, /legend:/);
	assert.match(panel, /selectedMode/);
	assert.match(panel, /media:/);
	assert.match(panel, /hideOverlap/);
	assert.doesNotMatch(panel, /<polyline|polylinePoints/);
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
	assert.match(graph, /node\.model/);
	assert.match(graph, /node\.workspaceName/);
	assert.match(recent, /Client/);
	assert.match(recent, /Method \/ Tool/);
	assert.match(recent, /In \/ Out/);
	assert.match(recent, /Status/);
	assert.match(recent, /When/);
	assert.match(recent, /clientModel/);
	assert.match(recent, /clientWorkspaceName/);
	assert.doesNotMatch(recent, /record\.arguments|record\.result|authorization|bearer token/i);
});

test('MCP topology graph renders directional flow lanes and latency health affordances', async () => {
	const graph = await read('lib/components/mcp/McpTopologyGraph.svelte');

	assert.match(graph, /edge-flow-gradient/);
	assert.match(graph, /edge-flow/);
	assert.match(graph, /edge-route-underlay/);
	assert.match(graph, /latencyTone/);
	assert.match(graph, /edge-health--healthy/);
	assert.match(graph, /edge-health--degraded/);
	assert.match(graph, /edge-health--error/);
	assert.match(graph, /node-selected-ring/);
	assert.match(graph, /@keyframes edge-flow/);
	assert.match(graph, /prefers-reduced-motion:\s*reduce/);
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
	correlation_id: null,
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

test('traffic and activity rows expose correlation metadata without changing row identity', async () => {
	let trafficState = reducer.hydrateMcpTraffic(snapshot());
	trafficState = reducer.applyMcpTrafficEvent(
		trafficState,
		trafficEvent(1, 'request_started', {
			request_id: 'request-correlated',
			correlation_id: 'corr-1'
		})
	);
	trafficState = reducer.applyMcpTrafficEvent(
		trafficState,
		trafficEvent(2, 'request_finished', {
			request_id: 'request-correlated',
			correlation_id: 'corr-1',
			status: 'complete'
		})
	);
	assert.equal(reducer.recentRequestRows(trafficState)[0].correlationId, 'corr-1');

	const activityReducer = await import(
		new URL('../src/lib/stores/mcp-activity.ts', import.meta.url)
	);
	const activityState = activityReducer.hydrateMcpActivity(
		activitySnapshot([
			activityEvent(1, 'started', { correlation_id: 'corr-1' }),
			activityEvent(2, 'complete', { correlation_id: 'corr-1' })
		])
	);
	assert.equal(activityState.rows[0].correlationId, 'corr-1');
	assert.equal(activityState.rows[0].correlationKey, 'plugin:request-1:cptr_list_workspaces');
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

test('MCP route and topology use NightOwl tokens, Back navigation, compact mobile requests, and full infrastructure path', async () => {
	const [page, topology, graph, recent, serverList, toolForm, card] = await Promise.all([
		read('routes/mcp/+page.svelte'),
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpTopologyGraph.svelte'),
		read('lib/components/mcp/McpRecentRequests.svelte'),
		read('lib/components/mcp/McpServerList.svelte'),
		read('lib/components/mcp/McpToolForm.svelte'),
		read('lib/components/mcp/McpCallCard.svelte')
	]);

	assert.match(page, /href=["']\/["']/);
	assert.match(page, /aria-label=["']Back to CPTR Home["']/);
	assert.match(page, /app-theme/);
	assert.match(page, /app-surface/);
	assert.match(graph, /MCP Connector/);
	assert.match(graph, /CPTR MCP/);
	assert.match(graph, /CPTR Backend/);
	assert.match(graph, /connectorY/);
	assert.match(graph, /backendY/);
	assert.match(graph, /traffic-particle/);
	assert.doesNotMatch(graph, /Unknown MCP Client/);
	assert.match(recent, /sm:hidden/);
	assert.match(recent, /hidden[^"\n]*sm:block/);
	assert.doesNotMatch(recent, /min-w-\[39rem\]/);
	assert.match(recent, /min-h-11/);

	const themedSource = [page, topology, graph, recent, serverList, toolForm, card].join('\n');
	for (const legacy of ['bg-white/80', 'bg-white/70', 'bg-gray-50/70', 'min-w-[39rem]']) {
		assert.equal(themedSource.includes(legacy), false, `must remove ${legacy}`);
	}
});

test('all MCP topology nodes and measured edges are selectable, aliasable, and keyboard accessible', async () => {
	const [topology, graph, detail] = await Promise.all([
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpTopologyGraph.svelte'),
		read('lib/components/mcp/McpTopologyDetail.svelte').catch(() => '')
	]);

	assert.match(topology, /getMcpTopologyConfig/);
	assert.match(topology, /getMcpDiagnosticsSnapshot/);
	assert.match(topology, /openMcpDiagnosticsStream/);
	assert.match(topology, /McpTopologyDetail/);
	assert.match(graph, /mcp-connector/);
	assert.match(graph, /cptr-mcp/);
	assert.match(graph, /cptr-backend/);
	assert.match(graph, /client-mcp-connector/);
	assert.match(graph, /mcp-connector-cptr-mcp/);
	assert.match(graph, /cptr-mcp-cptr-backend/);
	assert.match(graph, /role=["']button["']/);
	assert.match(graph, /tabindex=["']0["']/);
	assert.match(graph, /Enter/);
	assert.match(graph, /event\.key === ' '/);
	assert.match(graph, /Observed request time/);
	assert.match(graph, /Adapter handoff/);
	assert.match(graph, /Backend API RTT/);
	assert.match(detail, /updateMcpTopologyConfig/);
	assert.match(detail, /Save/);
	assert.match(detail, /Reset to default/);
	assert.match(detail, /Canonical ID/);
	assert.match(detail, /Canonical name/);
	assert.match(detail, /average/i);
	assert.match(detail, /p50/i);
	assert.match(detail, /p95/i);
	assert.match(detail, /max/i);
});

test('CPTR Backend detail exposes bounded live system monitoring and sparklines', async () => {
	const [topology, detail, monitor] = await Promise.all([
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpTopologyDetail.svelte').catch(() => ''),
		read('lib/components/mcp/McpBackendMonitor.svelte').catch(() => '')
	]);

	assert.match(detail, /McpBackendMonitor/);
	assert.match(detail, /cptr-backend/);
	for (const label of [
		'CPU',
		'RAM',
		'Disk',
		'Disk read',
		'Disk write',
		'Disk IOPS',
		'Network RX',
		'Network TX',
		'GPU',
		'GPU memory',
		'GPU temperature',
		'Uptime',
		'Processes',
		'Telemetry health'
	]) {
		assert.ok(monitor.includes(label), `backend monitor must show ${label}`);
	}
	assert.match(monitor, /Unavailable/);
	assert.match(monitor, /McpTimeSeriesChart/);
	assert.match(monitor, /Minimum|Average|Maximum/);
	assert.match(monitor, /app-surface|app-subtle-surface/);
	assert.doesNotMatch(monitor, /<polyline|sparkline/);
	assert.match(topology, /diagnostics/);
});

test('shared MCP time-series charts use modular ECharts with responsive lifecycle and accessibility', async () => {
	const [chart, packageJson] = await Promise.all([
		read('lib/components/mcp/McpTimeSeriesChart.svelte').catch(() => ''),
		read('../package.json')
	]);

	assert.match(packageJson, /["']echarts["']/);
	assert.match(chart, /echarts\/core/);
	assert.match(chart, /CanvasRenderer/);
	assert.match(chart, /LineChart/);
	assert.match(chart, /TooltipComponent/);
	assert.match(chart, /DataZoomComponent/);
	assert.match(chart, /ResizeObserver/);
	assert.match(chart, /prefers-reduced-motion/);
	assert.match(chart, /dispose\(/);
	assert.match(chart, /aria-label/);
	assert.match(chart, /notMerge:\s*false/);
	assert.doesNotMatch(chart, /notMerge:\s*true/);
	assert.doesNotMatch(chart, /import\s+\*\s+as\s+echarts/);
});

test('failed MCP requests show safe structured diagnostics and can reveal matching Activity', async () => {
	const [page, topology, recent, diagnostic, console, activity] = await Promise.all([
		read('routes/mcp/+page.svelte'),
		read('lib/components/mcp/McpTopology.svelte'),
		read('lib/components/mcp/McpRecentRequests.svelte'),
		read('lib/components/mcp/McpDiagnosticDetail.svelte').catch(() => ''),
		read('lib/components/mcp/McpConsole.svelte'),
		read('lib/components/mcp/McpActivityFeed.svelte')
	]);

	assert.match(recent, /McpDiagnosticDetail/);
	assert.match(recent, /correlationId/);
	assert.match(recent, /requestId/);
	assert.match(recent, /No deeper diagnostic was captured/);
	for (const label of [
		'Stage',
		'Error code',
		'HTTP status',
		'Retryable',
		'Duration',
		'Request ID',
		'Correlation ID',
		'Summary'
	]) {
		assert.ok(diagnostic.includes(label), `diagnostic detail must show ${label}`);
	}
	assert.match(diagnostic, /Show Activity/);
	assert.doesNotMatch(diagnostic, /stack|headers|authorization|cookie|arguments_json|result_json/i);
	assert.match(topology, /onrevealactivity/);
	assert.match(page, /focusRequestId/);
	assert.match(page, /focusCorrelationId/);
	assert.match(page, /view = 'console'/);
	assert.match(console, /focusRequestId/);
	assert.match(console, /focusCorrelationId/);
	assert.match(activity, /focusRequestId/);
	assert.match(activity, /focusCorrelationId/);
	assert.match(activity, /scrollIntoView/);
	assert.match(recent, /sm:hidden/);
	assert.doesNotMatch(recent, /min-w-\[39rem\]/);
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
