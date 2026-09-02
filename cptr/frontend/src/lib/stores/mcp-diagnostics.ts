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
	McpProcessMetrics,
	McpPricingStatus,
	McpUsageDiagnostic,
	McpUsagePeriodTotals,
	McpUsagePeriods,
	McpUsageTotals
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

export type McpUsageState = {
	eventId: string;
	timestampMs: number;
	requestId: string | null;
	correlationId: string | null;
	sessionId: string | null;
	clientId: string;
	modelReported: string | null;
	modelCanonical: string | null;
	modelSource: 'self_reported' | 'unavailable';
	toolName: string;
	inputTokensEstimated: number;
	outputTokensEstimated: number;
	cachedInputTokensEstimated: null;
	estimatorMethod: string;
	estimatorExactForModel: boolean;
	status: 'complete' | 'error';
	pricingStatus: McpPricingStatus;
	pricingVersion: string;
	pricingVerifiedAt: string;
	pricingValidThrough: string | null;
	pricingSourceLabel: string;
	pricingSourceUrl: string;
	inputUsdPerMillion: string | null;
	cachedInputUsdPerMillion: string | null;
	outputUsdPerMillion: string | null;
	inputCostUsd: number | null;
	cachedInputCostUsd: null;
	outputCostUsd: number | null;
	simulatedCostUsd: number | null;
};

export type McpUsageTotalsState = {
	inputTokensEstimated: number;
	outputTokensEstimated: number;
	totalTokensEstimated: number;
	simulatedCostUsd: number;
	pricedEvents: number;
	staleEvents: number;
	unpricedEvents: number;
};

export type McpUsagePeriodKey = 'week' | 'month' | 'rolling_7d' | 'rolling_30d' | 'all_time';
export type McpUsagePeriodTotalsState = McpUsageTotalsState & { requests: number };
export type McpUsagePeriodsState = Record<McpUsagePeriodKey, McpUsagePeriodTotalsState>;

export type McpUsageTimelineBucket = {
	startMs: number;
	endMs: number;
	inputTokens: number;
	outputTokens: number;
	totalTokens: number;
	simulatedCostUsd: number;
	requests: number;
};

export type McpDiagnosticsState = {
	sequence: number;
	latency: Partial<Record<McpLatencyEdge, McpLatencySummaryState>>;
	failures: McpFailureState[];
	system: McpBackendMetricsState[];
	usage: McpUsageState[];
	usageTotalsState: McpUsageTotalsState;
	usagePeriodsState: McpUsagePeriodsState;
	usagePeriodsGeneratedAtMs: number;
	latencyCapacityPerEdge: number;
	failureCapacity: number;
	systemCapacity: number;
	usageCapacity: number;
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

function decimalNumber(value: string | null): number | null {
	if (value == null) return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function usageTotalsState(
	totals: McpUsageTotals | undefined,
	retainedUsage: McpUsageState[]
): McpUsageTotalsState {
	if (totals) {
		return {
			inputTokensEstimated: totals.input_tokens_estimated,
			outputTokensEstimated: totals.output_tokens_estimated,
			totalTokensEstimated: totals.total_tokens_estimated,
			simulatedCostUsd: decimalNumber(totals.simulated_cost_usd) ?? 0,
			pricedEvents: totals.priced_events,
			staleEvents: totals.stale_events,
			unpricedEvents: totals.unpriced_events
		};
	}
	let inputTokensEstimated = 0;
	let outputTokensEstimated = 0;
	let simulatedCostUsd = 0;
	let pricedEvents = 0;
	let staleEvents = 0;
	let unpricedEvents = 0;
	for (const event of retainedUsage) {
		inputTokensEstimated += event.inputTokensEstimated;
		outputTokensEstimated += event.outputTokensEstimated;
		simulatedCostUsd += event.simulatedCostUsd ?? 0;
		if (event.pricingStatus === 'current') pricedEvents += 1;
		else if (event.pricingStatus === 'stale') staleEvents += 1;
		else unpricedEvents += 1;
	}
	return {
		inputTokensEstimated,
		outputTokensEstimated,
		totalTokensEstimated: inputTokensEstimated + outputTokensEstimated,
		simulatedCostUsd,
		pricedEvents,
		staleEvents,
		unpricedEvents
	};
}

function usagePeriodState(period: McpUsagePeriodTotals): McpUsagePeriodTotalsState {
	return {
		requests: period.requests,
		inputTokensEstimated: period.input_tokens_estimated,
		outputTokensEstimated: period.output_tokens_estimated,
		totalTokensEstimated: period.total_tokens_estimated,
		simulatedCostUsd: decimalNumber(period.simulated_cost_usd) ?? 0,
		pricedEvents: period.priced_events,
		staleEvents: period.stale_events,
		unpricedEvents: period.unpriced_events
	};
}

function usagePeriodsState(periods: McpUsagePeriods | undefined): McpUsagePeriodsState {
	if (!periods) {
		return {
			week: emptyUsagePeriod(),
			month: emptyUsagePeriod(),
			rolling_7d: emptyUsagePeriod(),
			rolling_30d: emptyUsagePeriod(),
			all_time: emptyUsagePeriod()
		};
	}
	return {
		week: usagePeriodState(periods.week),
		month: usagePeriodState(periods.month),
		rolling_7d: usagePeriodState(periods.rolling_7d),
		rolling_30d: usagePeriodState(periods.rolling_30d),
		all_time: usagePeriodState(periods.all_time)
	};
}

function emptyUsagePeriod(): McpUsagePeriodTotalsState {
	return {
		requests: 0,
		inputTokensEstimated: 0,
		outputTokensEstimated: 0,
		totalTokensEstimated: 0,
		simulatedCostUsd: 0,
		pricedEvents: 0,
		staleEvents: 0,
		unpricedEvents: 0
	};
}

function utcWeekKey(timestampMs: number): string {
	const date = new Date(timestampMs);
	const day = (date.getUTCDay() + 6) % 7;
	const monday = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() - day);
	return String(monday);
}

function utcMonthKey(timestampMs: number): string {
	const date = new Date(timestampMs);
	return `${date.getUTCFullYear()}-${date.getUTCMonth()}`;
}

function incrementPeriod(
	period: McpUsagePeriodTotalsState,
	event: McpUsageDiagnostic
): McpUsagePeriodTotalsState {
	const inputTokensEstimated = period.inputTokensEstimated + event.input_tokens_estimated;
	const outputTokensEstimated = period.outputTokensEstimated + event.output_tokens_estimated;
	return {
		requests: period.requests + 1,
		inputTokensEstimated,
		outputTokensEstimated,
		totalTokensEstimated: inputTokensEstimated + outputTokensEstimated,
		simulatedCostUsd: period.simulatedCostUsd + (decimalNumber(event.simulated_cost_usd) ?? 0),
		pricedEvents: period.pricedEvents + (event.pricing_status === 'current' ? 1 : 0),
		staleEvents: period.staleEvents + (event.pricing_status === 'stale' ? 1 : 0),
		unpricedEvents:
			period.unpricedEvents +
			(event.pricing_status === 'current' || event.pricing_status === 'stale' ? 0 : 1)
	};
}

function applyUsagePeriodEvent(
	state: McpDiagnosticsState,
	event: McpUsageDiagnostic
): McpUsagePeriodsState {
	const crossedWeek = utcWeekKey(event.timestamp_ms) !== utcWeekKey(state.usagePeriodsGeneratedAtMs);
	const crossedMonth = utcMonthKey(event.timestamp_ms) !== utcMonthKey(state.usagePeriodsGeneratedAtMs);
	return {
		...state.usagePeriodsState,
		week: incrementPeriod(crossedWeek ? emptyUsagePeriod() : state.usagePeriodsState.week, event),
		month: incrementPeriod(crossedMonth ? emptyUsagePeriod() : state.usagePeriodsState.month, event),
		rolling_7d: incrementPeriod(state.usagePeriodsState.rolling_7d, event),
		rolling_30d: incrementPeriod(state.usagePeriodsState.rolling_30d, event),
		all_time: incrementPeriod(state.usagePeriodsState.all_time, event)
	};
}

function usageState(event: McpUsageDiagnostic): McpUsageState {
	return {
		eventId: event.event_id,
		timestampMs: event.timestamp_ms,
		requestId: event.request_id,
		correlationId: event.correlation_id,
		sessionId: event.session_id,
		clientId: event.client_id,
		modelReported: event.model_reported,
		modelCanonical: event.model_canonical,
		modelSource: event.model_source,
		toolName: event.tool_name,
		inputTokensEstimated: event.input_tokens_estimated,
		outputTokensEstimated: event.output_tokens_estimated,
		cachedInputTokensEstimated: event.cached_input_tokens_estimated,
		estimatorMethod: event.estimator_method,
		estimatorExactForModel: event.estimator_exact_for_model,
		status: event.status,
		pricingStatus: event.pricing_status,
		pricingVersion: event.pricing_version,
		pricingVerifiedAt: event.pricing_verified_at,
		pricingValidThrough: event.pricing_valid_through,
		pricingSourceLabel: event.pricing_source_label,
		pricingSourceUrl: event.pricing_source_url,
		inputUsdPerMillion: event.input_usd_per_million,
		cachedInputUsdPerMillion: event.cached_input_usd_per_million,
		outputUsdPerMillion: event.output_usd_per_million,
		inputCostUsd: decimalNumber(event.input_cost_usd),
		cachedInputCostUsd: event.cached_input_cost_usd,
		outputCostUsd: decimalNumber(event.output_cost_usd),
		simulatedCostUsd: decimalNumber(event.simulated_cost_usd)
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
	const usageCapacity = Math.max(1, snapshot.stream_health.usage_capacity || 1);
	const retainedUsage = boundedTail((snapshot.usage ?? []).map(usageState), usageCapacity);
	return {
		sequence: snapshot.sequence,
		latency,
		failures: boundedTail(snapshot.failures.map(failureState), failureCapacity),
		system: boundedTail(snapshot.system.map(systemState), systemCapacity),
		usage: retainedUsage,
		usageTotalsState: usageTotalsState(snapshot.usage_totals, retainedUsage),
		usagePeriodsState: usagePeriodsState(snapshot.usage_periods),
		usagePeriodsGeneratedAtMs: snapshot.usage_periods?.generated_at_ms ?? Date.now(),
		latencyCapacityPerEdge: Math.max(
			1,
			snapshot.stream_health.latency_sample_capacity_per_edge || 1
		),
		failureCapacity,
		systemCapacity,
		usageCapacity,
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

	if (event.kind === 'usage') {
		const inputTokensEstimated =
			state.usageTotalsState.inputTokensEstimated + event.input_tokens_estimated;
		const outputTokensEstimated =
			state.usageTotalsState.outputTokensEstimated + event.output_tokens_estimated;
		return {
			...state,
			sequence: event.ingestion_sequence,
			usage: boundedTail([...state.usage, usageState(event)], state.usageCapacity),
			usagePeriodsState: applyUsagePeriodEvent(state, event),
			usagePeriodsGeneratedAtMs: event.timestamp_ms,
			usageTotalsState: {
				inputTokensEstimated,
				outputTokensEstimated,
				totalTokensEstimated: inputTokensEstimated + outputTokensEstimated,
				simulatedCostUsd:
					state.usageTotalsState.simulatedCostUsd + (decimalNumber(event.simulated_cost_usd) ?? 0),
				pricedEvents:
					state.usageTotalsState.pricedEvents + (event.pricing_status === 'current' ? 1 : 0),
				staleEvents:
					state.usageTotalsState.staleEvents + (event.pricing_status === 'stale' ? 1 : 0),
				unpricedEvents:
					state.usageTotalsState.unpricedEvents +
					(event.pricing_status === 'current' || event.pricing_status === 'stale' ? 0 : 1)
			}
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

export function currentUsageModel(state: McpDiagnosticsState | null): McpUsageState | null {
	return state?.usage.at(-1) ?? null;
}

export function usageTotals(state: McpDiagnosticsState): McpUsageTotalsState {
	return state.usageTotalsState;
}

export function usagePeriodTotals(
	state: McpDiagnosticsState,
	period: McpUsagePeriodKey
): McpUsagePeriodTotalsState {
	return state.usagePeriodsState[period];
}

export function usageTimeline(
	state: McpDiagnosticsState,
	nowMs: number,
	options: { windowMs?: number; bucketMs?: number } = {}
): McpUsageTimelineBucket[] {
	const requestedWindow = Number.isFinite(options.windowMs)
		? Math.floor(options.windowMs ?? 60_000)
		: 60_000;
	const requestedBucket = Number.isFinite(options.bucketMs)
		? Math.floor(options.bucketMs ?? 5_000)
		: 5_000;
	const windowMs = Math.min(3_600_000, Math.max(1_000, requestedWindow));
	const bucketMs = Math.min(windowMs, Math.max(1_000, requestedBucket));
	const bucketCount = Math.min(120, Math.max(1, Math.ceil(windowMs / bucketMs)));
	const effectiveWindow = bucketCount * bucketMs;
	const endMs = Math.max(0, Math.floor(nowMs));
	const startMs = endMs - effectiveWindow;
	const buckets: McpUsageTimelineBucket[] = Array.from({ length: bucketCount }, (_, index) => ({
		startMs: startMs + index * bucketMs,
		endMs: startMs + (index + 1) * bucketMs,
		inputTokens: 0,
		outputTokens: 0,
		totalTokens: 0,
		simulatedCostUsd: 0,
		requests: 0
	}));

	for (const event of state.usage) {
		if (event.timestampMs < startMs || event.timestampMs > endMs) continue;
		const rawIndex = Math.floor((event.timestampMs - startMs) / bucketMs);
		const index = Math.min(bucketCount - 1, Math.max(0, rawIndex));
		const bucket = buckets[index];
		bucket.inputTokens += event.inputTokensEstimated;
		bucket.outputTokens += event.outputTokensEstimated;
		bucket.totalTokens += event.inputTokensEstimated + event.outputTokensEstimated;
		bucket.simulatedCostUsd += event.simulatedCostUsd ?? 0;
		bucket.requests += 1;
	}
	return buckets;
}
