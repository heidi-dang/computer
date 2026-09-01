/**
 * MCP (Model Context Protocol) API — typed wrappers for /api/mcp/* endpoints.
 */
import { fetchJSON, fetchHandler, jsonBody } from '$lib/apis';
import { consumeMcpSseBuffer } from '$lib/utils/mcp-console';

// ── Types ────────────────────────────────────────────────────────────────────

export interface McpServer {
	id: string;
	name: string;
	type: 'mcp' | 'mcp_stdio' | string;
	url: string | null;
	command: string | null;
	enabled: boolean;
	health: 'connected' | 'disconnected' | 'timeout' | 'error' | 'unknown' | 'n/a' | string;
}

export interface McpToolSpec {
	name: string;
	description: string;
	parameters: Record<string, unknown>; // JSON Schema object
	_server_id?: string;
	_server_name?: string;
}

export interface McpContentItem {
	type: 'text' | 'image' | 'resource' | string;
	text?: string;
	data?: string; // base64 for images
	mimeType?: string;
	uri?: string;
}

export interface McpResource {
	uri: string;
	name?: string;
	description?: string;
	mimeType?: string;
}

export type McpActivityPhase = 'started' | 'complete' | 'failed';

export interface McpActivityEvent {
	version: 1;
	event_id: string;
	sequence: number;
	ingestion_sequence: number;
	timestamp_ms: number;
	client: McpTrafficClient;
	session_id: string | null;
	request_id: string | null;
	correlation_id: string | null;
	tool_name: string;
	title: string | null;
	phase: McpActivityPhase;
	summary: string;
	arguments_json: string | null;
	result_json: string | null;
	error_json: string | null;
	duration_ms: number | null;
}

export interface McpActivitySnapshot {
	version: 1;
	sequence: number;
	events: McpActivityEvent[];
	stream_health: {
		subscriber_count: number;
		slow_subscriber_drops: number;
		event_capacity: number;
		subscriber_queue_capacity?: number;
	};
}

export interface McpActivityStreamCallbacks {
	onSnapshot: (snapshot: McpActivitySnapshot) => void;
	onActivity: (event: McpActivityEvent) => void;
	onOpen?: () => void;
	onError?: (error: unknown) => void;
}

export type McpTrafficEventType =
	| 'session_opened'
	| 'session_closed'
	| 'request_started'
	| 'request_finished'
	| 'request_failed'
	| 'tool_started'
	| 'tool_finished'
	| 'tool_failed';

export type McpTrafficStatus = 'started' | 'complete' | 'error' | 'connected' | 'disconnected';

export type McpTrafficErrorCode =
	| 'timeout'
	| 'validation_error'
	| 'unauthorized'
	| 'tool_error'
	| 'transport_error'
	| 'internal_error';

export interface McpTrafficClient {
	id: string;
	label: string;
	version: string | null;
}

export interface McpTrafficEvent {
	version: 1;
	event_id: string;
	sequence: number;
	ingestion_sequence: number;
	event_type: McpTrafficEventType;
	timestamp_ms: number;
	session_id: string | null;
	client: McpTrafficClient;
	request_id: string | null;
	correlation_id: string | null;
	method: string | null;
	tool_name: string | null;
	status: McpTrafficStatus;
	duration_ms: number | null;
	request_bytes: number | null;
	response_bytes: number | null;
	error_code: McpTrafficErrorCode | null;
}

export interface McpTrafficClientSnapshot {
	id: string;
	label: string;
	version: string | null;
	active_sessions: number;
	active_requests: number;
	total_requests: number;
	errors: number;
	last_seen: number;
	last_tool: string | null;
}

export interface McpTrafficSessionSnapshot {
	session_id: string;
	client_id: string;
	connected_at: number;
	last_seen: number;
}

export interface McpTrafficSnapshot {
	version: 1;
	sequence: number;
	center: { id: string; label: string; status: string };
	clients: McpTrafficClientSnapshot[];
	sessions: McpTrafficSessionSnapshot[];
	events: McpTrafficEvent[];
	stream_health: {
		subscriber_count: number;
		slow_subscriber_drops: number;
		session_evictions: number;
		request_evictions: number;
		expired_sessions: number;
		event_capacity: number;
		session_capacity: number;
	};
}

export interface McpTrafficStreamCallbacks {
	onSnapshot: (snapshot: McpTrafficSnapshot) => void;
	onTraffic: (event: McpTrafficEvent) => void;
	onOpen?: () => void;
	onError?: (error: unknown) => void;
}

export interface McpTopologyConfig {
	version: 1;
	canonical_labels: Record<string, string>;
	aliases: Record<string, string>;
}

export type McpLatencyEdge =
	| 'client-mcp-connector'
	| 'mcp-connector-cptr-mcp'
	| 'cptr-mcp-cptr-backend';
export type McpLatencyMetric = 'observed_request_time' | 'adapter_handoff' | 'backend_api_rtt';
export type McpFailureStage =
	| 'client_transport'
	| 'mcp_connector'
	| 'cptr_mcp'
	| 'cptr_backend'
	| 'activity_delivery'
	| 'traffic_delivery';

export interface McpLatencySample {
	kind: 'latency';
	version: 1;
	event_id: string;
	timestamp_ms: number;
	request_id: string | null;
	correlation_id: string | null;
	edge_id: McpLatencyEdge;
	metric_type: McpLatencyMetric;
	duration_ms: number;
	status: 'ok' | 'error';
}

export interface McpLatencyAggregate {
	metric_type: McpLatencyMetric;
	latest_ms: number;
	average_ms: number;
	p50_ms: number;
	p95_ms: number;
	max_ms: number;
	sample_count: number;
	last_updated_ms: number;
	latest_status: 'ok' | 'error';
	health: 'healthy' | 'degraded' | 'error';
}

export interface McpFailureDiagnostic {
	kind: 'failure';
	version: 1;
	diagnostic_id: string;
	request_id: string | null;
	correlation_id: string | null;
	session_id: string | null;
	client_id: string;
	method: string | null;
	tool_name: string | null;
	stage: McpFailureStage;
	error_code: string;
	http_status: number | null;
	retryable: boolean | null;
	started_at_ms: number | null;
	completed_at_ms: number;
	duration_ms: number | null;
	request_bytes: number | null;
	response_bytes: number | null;
	summary: string;
}

export interface McpGpuMetrics {
	index: number;
	name: string;
	utilization_percent: number;
	memory_used_bytes: number;
	memory_total_bytes: number;
	temperature_c: number | null;
}

export interface McpProcessMetrics {
	pid: number;
	cpu_percent: number | null;
	memory_percent: number | null;
	name: string;
}

export interface McpBackendMetricsSample {
	kind: 'system';
	version: 1;
	timestamp_ms: number;
	cpu_usage_percent: number | null;
	cpu_count: number;
	load_avg: number[];
	memory_total_bytes: number | null;
	memory_available_bytes: number | null;
	disk_total_bytes: number | null;
	disk_used_bytes: number | null;
	disk_free_bytes: number | null;
	disk_read_bytes_per_s: number | null;
	disk_write_bytes_per_s: number | null;
	disk_read_ops_per_s: number | null;
	disk_write_ops_per_s: number | null;
	network_rx_bytes_per_s: number | null;
	network_tx_bytes_per_s: number | null;
	uptime_seconds: number | null;
	gpu_status: 'available' | 'unavailable' | 'error';
	gpus: McpGpuMetrics[];
	cptr_process: McpProcessMetrics | null;
	processes: McpProcessMetrics[];
}

export type McpPricingStatus = 'current' | 'stale' | 'unknown_model' | 'model_not_reported';

export interface McpUsageDiagnostic {
	kind: 'usage';
	version: 1;
	event_id: string;
	timestamp_ms: number;
	request_id: string | null;
	correlation_id: string | null;
	session_id: string | null;
	client_id: string;
	model_reported: string | null;
	model_canonical: string | null;
	model_source: 'self_reported' | 'unavailable';
	tool_name: string;
	input_tokens_estimated: number;
	output_tokens_estimated: number;
	cached_input_tokens_estimated: null;
	estimator_method: string;
	estimator_exact_for_model: boolean;
	status: 'complete' | 'error';
	pricing_status: McpPricingStatus;
	pricing_version: string;
	pricing_verified_at: string;
	pricing_valid_through: string | null;
	pricing_source_label: string;
	pricing_source_url: string;
	input_usd_per_million: string | null;
	cached_input_usd_per_million: string | null;
	output_usd_per_million: string | null;
	input_cost_usd: string | null;
	cached_input_cost_usd: null;
	output_cost_usd: string | null;
	simulated_cost_usd: string | null;
}

export interface McpUsageModelTotals {
	events: number;
	input_tokens_estimated: number;
	output_tokens_estimated: number;
	total_tokens_estimated: number;
	simulated_cost_usd: string;
}

export interface McpUsageTotals {
	input_tokens_estimated: number;
	output_tokens_estimated: number;
	total_tokens_estimated: number;
	simulated_cost_usd: string;
	priced_events: number;
	stale_events: number;
	unpriced_events: number;
	by_model: Record<string, McpUsageModelTotals>;
}

export type McpDiagnosticsEvent = (
	| McpLatencySample
	| McpFailureDiagnostic
	| McpBackendMetricsSample
	| McpUsageDiagnostic
) & { ingestion_sequence: number };

export interface McpDiagnosticsSnapshot {
	version: 1;
	sequence: number;
	latency: Partial<Record<McpLatencyEdge, McpLatencyAggregate>>;
	failures: McpFailureDiagnostic[];
	system: McpBackendMetricsSample[];
	usage: McpUsageDiagnostic[];
	current_model: McpUsageDiagnostic | null;
	usage_totals: McpUsageTotals;
	stream_health: {
		subscriber_count: number;
		slow_subscriber_drops: number;
		latency_sample_capacity_per_edge: number;
		failure_capacity: number;
		system_sample_capacity: number;
		usage_capacity: number;
		subscriber_queue_capacity: number;
	};
}

export interface McpDiagnosticsStreamCallbacks {
	onSnapshot: (snapshot: McpDiagnosticsSnapshot) => void;
	onLatency: (event: McpLatencySample & { ingestion_sequence: number }) => void;
	onFailure: (event: McpFailureDiagnostic & { ingestion_sequence: number }) => void;
	onSystem: (event: McpBackendMetricsSample & { ingestion_sequence: number }) => void;
	onUsage: (event: McpUsageDiagnostic & { ingestion_sequence: number }) => void;
	onOpen?: () => void;
	onError?: (error: unknown) => void;
}

// ── Topology config + diagnostics ────────────────────────────────────────────

export const getMcpTopologyConfig = () => fetchJSON<McpTopologyConfig>('/api/mcp/topology/config');

export const updateMcpTopologyConfig = (aliases: Record<string, string | null>) =>
	fetchJSON<McpTopologyConfig>('/api/mcp/topology/config', {
		...jsonBody({ aliases }),
		method: 'PUT'
	});

export const getMcpDiagnosticsSnapshot = () =>
	fetchJSON<McpDiagnosticsSnapshot>('/api/mcp/diagnostics/snapshot');

export function openMcpDiagnosticsStream(callbacks: McpDiagnosticsStreamCallbacks): () => void {
	const source = new EventSource('/api/mcp/diagnostics/stream');
	const parse = <T>(message: MessageEvent<string>, callback: (value: T) => void) => {
		try {
			callback(JSON.parse(message.data) as T);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) =>
		parse(event as MessageEvent<string>, callbacks.onSnapshot)
	);
	source.addEventListener('latency', (event) =>
		parse(event as MessageEvent<string>, callbacks.onLatency)
	);
	source.addEventListener('failure', (event) =>
		parse(event as MessageEvent<string>, callbacks.onFailure)
	);
	source.addEventListener('system', (event) =>
		parse(event as MessageEvent<string>, callbacks.onSystem)
	);
	source.addEventListener('usage', (event) =>
		parse(event as MessageEvent<string>, callbacks.onUsage)
	);
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);

	return () => source.close();
}

// ── Live tool activity ───────────────────────────────────────────────────────

export const getMcpActivitySnapshot = () =>
	fetchJSON<McpActivitySnapshot>('/api/mcp/activity/snapshot');

export function openMcpActivityStream(callbacks: McpActivityStreamCallbacks): () => void {
	const source = new EventSource('/api/mcp/activity/stream');

	const parse = <T>(message: MessageEvent<string>, callback: (value: T) => void) => {
		try {
			callback(JSON.parse(message.data) as T);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) =>
		parse(event as MessageEvent<string>, callbacks.onSnapshot)
	);
	source.addEventListener('activity', (event) =>
		parse(event as MessageEvent<string>, callbacks.onActivity)
	);
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);

	return () => source.close();
}

// ── Traffic topology ─────────────────────────────────────────────────────────

export const getMcpTrafficSnapshot = () =>
	fetchJSON<McpTrafficSnapshot>('/api/mcp/traffic/snapshot');

export function openMcpTrafficStream(callbacks: McpTrafficStreamCallbacks): () => void {
	const source = new EventSource('/api/mcp/traffic/stream');

	const parse = <T>(message: MessageEvent<string>, callback: (value: T) => void) => {
		try {
			callback(JSON.parse(message.data) as T);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) =>
		parse(event as MessageEvent<string>, callbacks.onSnapshot)
	);
	source.addEventListener('traffic', (event) =>
		parse(event as MessageEvent<string>, callbacks.onTraffic)
	);
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);

	return () => source.close();
}

// ── Server management ────────────────────────────────────────────────────────

export const listMcpServers = () =>
	fetchJSON<{ servers: McpServer[] }>('/api/mcp/servers').then((r) => r.servers);

export const getMcpServerStatus = (serverId: string) =>
	fetchJSON<{ server_id: string; type: string; status: string }>(
		`/api/mcp/servers/${serverId}/status`
	);

export const reconnectMcpServer = (serverId: string) =>
	fetchJSON(`/api/mcp/servers/${serverId}/reconnect`, { method: 'POST' });

export const getMcpServerLogs = (serverId: string, limit = 200) =>
	fetchJSON<{ server_id: string; lines: string[]; total_buffered: number }>(
		`/api/mcp/servers/${serverId}/logs?limit=${limit}`
	);

// ── Tool discovery ────────────────────────────────────────────────────────────

export const listServerTools = (serverId: string) =>
	fetchJSON<{ server_id: string; tools: McpToolSpec[] }>(`/api/mcp/servers/${serverId}/tools`).then(
		(r) => r.tools
	);

export const listAllTools = () =>
	fetchJSON<{ tools: McpToolSpec[]; count: number }>('/api/mcp/tools').then((r) => r.tools);

export const getToolSchema = (toolName: string) =>
	fetchJSON<McpToolSpec>(`/api/mcp/tools/${encodeURIComponent(toolName)}`);

// ── Tool invocation ──────────────────────────────────────────────────────────

/** Invoke a tool — returns the raw MCP content array. */
export const invokeTool = (
	serverId: string,
	toolName: string,
	args: Record<string, unknown> = {}
) =>
	fetchJSON<{ server_id: string; tool: string; result: McpContentItem[] }>(
		`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke`,
		jsonBody({ arguments: args })
	).then((r) => r.result);

/**
 * Invoke a tool with real SSE streaming via ?stream=1.
 * Emits events as they arrive: onStart → onChunk* → onDone | onError.
 */
export async function invokeToolStreaming(
	serverId: string,
	toolName: string,
	args: Record<string, unknown>,
	callbacks: {
		onStart?: () => void;
		onChunk?: (item: McpContentItem) => void;
		onDone?: (result: McpContentItem[]) => void;
		onError?: (message: string) => void;
	}
): Promise<void> {
	callbacks.onStart?.();

	let res: Response;
	try {
		res = await fetch(
			`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke?stream=1`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ arguments: args })
			}
		);
	} catch (err: unknown) {
		callbacks.onError?.(err instanceof Error ? err.message : String(err));
		return;
	}

	if (!res.ok) {
		const data = await res.json().catch(() => ({}));
		callbacks.onError?.(data.detail || data.error || res.statusText);
		return;
	}

	const reader = res.body?.getReader();
	if (!reader) {
		callbacks.onError?.('No response body');
		return;
	}

	const decoder = new TextDecoder();
	let buffer = '';

	const dispatchFrames = (frames: ReturnType<typeof consumeMcpSseBuffer>['frames']) => {
		for (const frame of frames) {
			if (frame.event === 'tool_chunk') {
				callbacks.onChunk?.(frame.data as McpContentItem);
			} else if (frame.event === 'tool_done') {
				const payload = frame.data as { result?: McpContentItem[] };
				callbacks.onDone?.(payload.result ?? []);
			} else if (frame.event === 'tool_error') {
				const payload = frame.data as { message?: string };
				callbacks.onError?.(payload.message ?? 'Unknown error');
			}
		}
	};

	while (true) {
		const { done, value } = await reader.read();
		if (done) {
			buffer += decoder.decode();
			const consumed = consumeMcpSseBuffer(buffer, true);
			dispatchFrames(consumed.frames);
			break;
		}
		buffer += decoder.decode(value, { stream: true });
		const consumed = consumeMcpSseBuffer(buffer);
		buffer = consumed.remainder;
		dispatchFrames(consumed.frames);
	}
}

// ── Resources ────────────────────────────────────────────────────────────────

export const listServerResources = (serverId: string) =>
	fetchJSON<{ server_id: string; resources: McpResource[] }>(
		`/api/mcp/servers/${serverId}/resources/list`,
		{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
	).then((r) => r.resources);

export const readServerResource = (serverId: string, uri: string) =>
	fetchJSON<{ server_id: string; uri: string; contents: McpContentItem[] }>(
		`/api/mcp/servers/${serverId}/resources/read`,
		jsonBody({ uri })
	).then((r) => r.contents);
