import type {
	McpBackendMetricsSample,
	McpDiagnosticsEvent,
	McpDiagnosticsSnapshot,
	McpFailureDiagnostic,
	McpFailureStage,
	McpGpuMetrics,
	McpLatencyAggregate,
	McpLatencyEdge,
	McpLatencyMetric,
	McpProcessMetrics
} from '$lib/apis/mcp';

export type McpLatencySummaryState = {
	edgeId: McpLatencyEdge;
	metricType: McpLatencyMetric;
	latestMs: number;
	averageMs: number;
	p50Ms: number;
	p95Ms: number;
	maxMs: number;
	sampleCount: number;
	lastUpdatedMs: number;
	latestStatus: 'ok' | 'error';
	health: 'healthy' | 'degraded' | 'error';
};

export type McpFailureState = {
	diagnosticId: string;
	requestId: string | null;
	correlationId: string | null;
	sessionId: string | null;
	clientId: string;
	method: string | null;
	toolName: string | null;
	stage: McpFailureStage;
	errorCode: string;
	httpStatus: number | null;
	retryable: boolean | null;
	startedAtMs: number | null;
	completedAtMs: number;
	durationMs: number | null;
	requestBytes: number | null;
	responseBytes: number | null;
	summary: string;
};

export type McpGpuMetricsState = {
	index: number;
	name: string;
	utilizationPercent: number;
	memoryUsedBytes: number;
	memoryTotalBytes: number;
	temperatureC: number | null;
};

export type McpProcessMetricsState = {
	pid: number;
	cpuPercent: number | null;
	memoryPercent: number | null;
	name: string;
};

export type McpBackendMetricsState = {
	timestampMs: number;
	cpuUsagePercent: number | null;
	cpuCount: number;
	loadAvg: number[];
	memoryTotalBytes: number | null;
	memoryAvailableBytes: number | null;
	diskTotalBytes: number | null;
	diskUsedBytes: number | null;
	diskFreeBytes: number | null;
	diskReadBytesPerS: number | null;
	diskWriteBytesPerS: number | null;
	diskReadOpsPerS: number | null;
	diskWriteOpsPerS: number | null;
	networkRxBytesPerS: number | null;
	networkTxBytesPerS: number | null;
	uptimeSeconds: number | null;
	gpuStatus: 'available' | 'unavailable' | 'error';
	gpus: McpGpuMetricsState[];
	cptrProcess: McpProcessMetricsState | null;
	processes: McpProcessMetricsState[];
};

export type McpDiagnosticsState = {
	sequence: number;
	latency: Partial<Record<McpLatencyEdge, McpLatencySummaryState>>;
	failures: McpFailureState[];
	system: McpBackendMetricsState[];
	latencyCapacityPerEdge: number;
	failureCapacity: number;
	systemCapacity: number;
	subscriberQueueCapacity: number;
	streamHealth: {
		subscriberCount: number;
		slowSubscriberDrops: number;
	};
};

function boundedTail<T>(items: T[], limit: number): T[] {
	return items.length <= limit ? items : items.slice(items.length - limit);
}

function latencyState(
	edgeId: McpLatencyEdge,
	aggregate: McpLatencyAggregate
): McpLatencySummaryState {
	return {
		edgeId,
		metricType: aggregate.metric_type,
		latestMs: aggregate.latest_ms,
		averageMs: aggregate.average_ms,
		p50Ms: aggregate.p50_ms,
		p95Ms: aggregate.p95_ms,
		maxMs: aggregate.max_ms,
		sampleCount: aggregate.sample_count,
		lastUpdatedMs: aggregate.last_updated_ms,
		latestStatus: aggregate.latest_status,
		health: aggregate.health
	};
}

function failureState(event: McpFailureDiagnostic): McpFailureState {
	return {
		diagnosticId: event.diagnostic_id,
		requestId: event.request_id,
		correlationId: event.correlation_id,
		sessionId: event.session_id,
		clientId: event.client_id,
		method: event.method,
		toolName: event.tool_name,
		stage: event.stage,
		errorCode: event.error_code,
		httpStatus: event.http_status,
		retryable: event.retryable,
		startedAtMs: event.started_at_ms,
		completedAtMs: event.completed_at_ms,
		durationMs: event.duration_ms,
		requestBytes: event.request_bytes,
		responseBytes: event.response_bytes,
		summary: event.summary
	};
}

function gpuState(gpu: McpGpuMetrics): McpGpuMetricsState {
	return {
		index: gpu.index,
		name: gpu.name,
		utilizationPercent: gpu.utilization_percent,
		memoryUsedBytes: gpu.memory_used_bytes,
		memoryTotalBytes: gpu.memory_total_bytes,
		temperatureC: gpu.temperature_c
	};
}

function processState(process: McpProcessMetrics): McpProcessMetricsState {
	return {
		pid: process.pid,
		cpuPercent: process.cpu_percent,
		memoryPercent: process.memory_percent,
		name: process.name
	};
}

function systemState(sample: McpBackendMetricsSample): McpBackendMetricsState {
	return {
		timestampMs: sample.timestamp_ms,
		cpuUsagePercent: sample.cpu_usage_percent,
		cpuCount: sample.cpu_count,
		loadAvg: [...sample.load_avg],
		memoryTotalBytes: sample.memory_total_bytes,
		memoryAvailableBytes: sample.memory_available_bytes,
		diskTotalBytes: sample.disk_total_bytes,
		diskUsedBytes: sample.disk_used_bytes,
		diskFreeBytes: sample.disk_free_bytes,
		diskReadBytesPerS: sample.disk_read_bytes_per_s,
		diskWriteBytesPerS: sample.disk_write_bytes_per_s,
		diskReadOpsPerS: sample.disk_read_ops_per_s,
		diskWriteOpsPerS: sample.disk_write_ops_per_s,
		networkRxBytesPerS: sample.network_rx_bytes_per_s,
		networkTxBytesPerS: sample.network_tx_bytes_per_s,
		uptimeSeconds: sample.uptime_seconds,
		gpuStatus: sample.gpu_status,
		gpus: sample.gpus.map(gpuState),
		cptrProcess: sample.cptr_process ? processState(sample.cptr_process) : null,
		processes: sample.processes.map(processState)
	};
}

export function hydrateMcpDiagnostics(snapshot: McpDiagnosticsSnapshot): McpDiagnosticsState {
	const latency: Partial<Record<McpLatencyEdge, McpLatencySummaryState>> = {};
	for (const [edgeId, aggregate] of Object.entries(snapshot.latency)) {
		if (!aggregate) continue;
		latency[edgeId as McpLatencyEdge] = latencyState(edgeId as McpLatencyEdge, aggregate);
	}
	const failureCapacity = Math.max(1, snapshot.stream_health.failure_capacity || 1);
	const systemCapacity = Math.max(1, snapshot.stream_health.system_sample_capacity || 1);
	return {
		sequence: snapshot.sequence,
		latency,
		failures: boundedTail(snapshot.failures.map(failureState), failureCapacity),
		system: boundedTail(snapshot.system.map(systemState), systemCapacity),
		latencyCapacityPerEdge: Math.max(
			1,
			snapshot.stream_health.latency_sample_capacity_per_edge || 1
		),
		failureCapacity,
		systemCapacity,
		subscriberQueueCapacity: Math.max(1, snapshot.stream_health.subscriber_queue_capacity || 1),
		streamHealth: {
			subscriberCount: snapshot.stream_health.subscriber_count,
			slowSubscriberDrops: snapshot.stream_health.slow_subscriber_drops
		}
	};
}

export function applyMcpDiagnosticsEvent(
	state: McpDiagnosticsState,
	event: McpDiagnosticsEvent
): McpDiagnosticsState {
	if (event.ingestion_sequence <= state.sequence) return state;

	if (event.kind === 'failure') {
		return {
			...state,
			sequence: event.ingestion_sequence,
			failures: boundedTail([...state.failures, failureState(event)], state.failureCapacity)
		};
	}

	if (event.kind === 'system') {
		return {
			...state,
			sequence: event.ingestion_sequence,
			system: boundedTail([...state.system, systemState(event)], state.systemCapacity)
		};
	}

	const current = state.latency[event.edge_id];
	const sampleCount = (current?.sampleCount ?? 0) + 1;
	const averageMs = current
		? (current.averageMs * current.sampleCount + event.duration_ms) / sampleCount
		: event.duration_ms;
	const next: McpLatencySummaryState = {
		edgeId: event.edge_id,
		metricType: event.metric_type,
		latestMs: event.duration_ms,
		averageMs,
		p50Ms: current?.p50Ms ?? event.duration_ms,
		p95Ms: current?.p95Ms ?? event.duration_ms,
		maxMs: Math.max(current?.maxMs ?? 0, event.duration_ms),
		sampleCount,
		lastUpdatedMs: event.timestamp_ms,
		latestStatus: event.status,
		health: event.status === 'error' ? 'error' : (current?.health ?? 'healthy')
	};
	return {
		...state,
		sequence: event.ingestion_sequence,
		latency: { ...state.latency, [event.edge_id]: next }
	};
}

export function latestBackendMetrics(
	state: McpDiagnosticsState | null
): McpBackendMetricsState | null {
	return state?.system.at(-1) ?? null;
}
