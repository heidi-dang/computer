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

export interface McpActivityClient {
	id: string;
	label: string;
	version: string | null;
}

export interface McpActivityEvent {
	version: 1;
	event_id: string;
	sequence: number;
	ingestion_sequence: number;
	timestamp_ms: number;
	client: McpActivityClient;
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
	session_name: string | null;
	model: string | null;
	workspace_id: string | null;
	workspace_name: string | null;
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
	session_name: string | null;
	model: string | null;
	workspace_id: string | null;
	workspace_name: string | null;
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
	'client-mcp-connector' | 'mcp-connector-cptr-mcp' | 'cptr-mcp-cptr-backend';
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

export interface McpUsagePeriodTotals {
	requests: number;
	input_tokens_estimated: number;
	output_tokens_estimated: number;
	total_tokens_estimated: number;
	simulated_cost_usd: string;
	priced_events: number;
	stale_events: number;
	unpriced_events: number;
}

export interface McpUsagePeriods {
	week: McpUsagePeriodTotals;
	month: McpUsagePeriodTotals;
	rolling_7d: McpUsagePeriodTotals;
	rolling_30d: McpUsagePeriodTotals;
	all_time: McpUsagePeriodTotals;
	generated_at_ms: number;
	timezone: 'UTC';
	week_starts_on: 'monday';
}

export interface McpEngineeringSession {
	session_id: string | null;
	client_id: string;
	model_reported: string | null;
	model_canonical: string | null;
	first_seen_ms: number;
	last_seen_ms: number;
	tool_calls: number;
	successful_tool_calls: number;
	failed_tool_calls: number;
	coding_mutations: number;
	verification_calls: number;
	read_calls: number;
	input_tokens_estimated: number;
	output_tokens_estimated: number;
	total_tokens_estimated: number;
	simulated_cost_usd: string;
	reliability: number;
	verification_ratio: number;
	operational_score: number;
	comparable: false;
}

export interface McpEngineeringSessionsResponse {
	comparable: false;
	comparability: 'observed_real_work_only';
	score_formula: string;
	disclaimer: string;
	sessions: McpEngineeringSession[];
}

export interface McpBenchmarkLeaderboardModel {
	model_canonical: string;
	model_reported: string | null;
	attempts: number;
	best_score: number;
	average_score: number;
	perfect_runs: number;
	pass_rate: number;
	median_duration_ms: number;
}

export interface McpBenchmarkLeaderboard {
	comparable: true;
	comparability: 'standardized_suite_only';
	suite_id: string;
	suite_version: string;
	max_score: number;
	models: McpBenchmarkLeaderboardModel[];
}

export interface McpFactoryRunSummary {
	run_id: string;
	workspace_id: string;
	workspace_name: string | null;
	mission: string;
	state: string;
	terminal: boolean;
	current_cycle_id: string | null;
	next_action: string | null;
	created_at: number;
	updated_at: number;
	completed_at: number | null;
}

export interface McpFactoryCycle {
	cycle_id: string;
	ordinal: number;
	state: string;
	selected_finding: unknown;
	capability_requirements: unknown;
	selected_capabilities: unknown;
	gate_plan: unknown;
	base_revision: string | null;
	base_fingerprint: string | null;
	target_revision: string | null;
	target_fingerprint: string | null;
	mutation_worker_id: string | null;
	attempt_count: number;
	failure_signatures: unknown;
	next_action: string | null;
	created_at: number;
	updated_at: number;
	completed_at: number | null;
}

export interface McpFactoryGate {
	gate_result_id: string;
	cycle_id: string;
	gate_id: string;
	category: string;
	required: boolean;
	applicable: boolean;
	status: string;
	evidence_ids: string[];
	evaluated_revision: string | null;
	evaluated_fingerprint: string | null;
	reason: string | null;
	attempt: number;
	created_at: number;
	updated_at: number;
}

export interface McpFactoryEvent {
	event_id: string;
	cycle_id: string | null;
	sequence: number;
	actor: string;
	event_type: string;
	from_state: string | null;
	to_state: string | null;
	payload: unknown;
	created_at: number;
}

export interface McpFactoryEvidence {
	evidence_id: string;
	cycle_id: string | null;
	gate_id: string | null;
	kind: string;
	source: string;
	authority: string;
	revision: string | null;
	fingerprint: string | null;
	digest: string;
	payload: unknown;
	created_at: number;
}

export interface McpFactoryWorker {
	assignment_id: string;
	cycle_id: string;
	worker_id: string | null;
	mode: string;
	repo_path: string;
	scope: string[];
	branch: string | null;
	base_revision: string | null;
	status: string;
	created_at: number;
	updated_at: number;
	closed_at: number | null;
}

export interface McpFactoryReasoning {
	reasoning_id: string;
	cycle_id: string;
	role: string;
	role_ordinal: number;
	schema_id: string;
	provider: string;
	model: string;
	response_id: string | null;
	input_tokens: number;
	output_tokens: number;
	total_tokens: number;
	runtime_ms: number;
	cost_microusd: number;
	attempt_count: number;
	data: unknown;
	provider_metadata: unknown;
	created_at: number;
}

export interface McpFactoryApproval {
	approval_id: string;
	cycle_id: string;
	kind: string;
	revision: string;
	remote: string;
	branch: string;
	status: string;
	note: string | null;
	created_at: number;
	updated_at: number;
	decided_at: number | null;
}

export interface McpFactoryMetric {
	metric_id: string;
	cycle_id: string | null;
	scope: string;
	dimension_key: string;
	attempts: number;
	repair_iterations: number;
	regressions: number;
	input_tokens: number;
	output_tokens: number;
	runtime_ms: number;
	cost_microusd: number;
	gate_latency_ms: number;
	verified_outcome: string | null;
	updated_at: number;
}

export interface McpFactoryCapabilityOutcome {
	outcome_id: string;
	cycle_id: string;
	capability_id: string;
	stable_id: string;
	version: string;
	origin_type: string;
	risk_classification: string;
	trust_status: string;
	verification_status: string;
	repository_family: string;
	task_family: string;
	verified_success: boolean;
	regression: boolean;
	repair_iterations: number;
	input_tokens: number;
	output_tokens: number;
	runtime_ms: number;
	cost_microusd: number;
	created_at: number;
}

export interface McpFactoryCommitIntent {
	commit_intent_id: string;
	cycle_id: string;
	repository_key: string;
	verified_revision: string;
	verified_fingerprint: string;
	diff_digest: string;
	changed_paths: string[];
	commit_message: string;
	status: string;
	commit_sha: string | null;
	push_status: string | null;
	push_remote: string | null;
	push_branch: string | null;
	push_approval_id: string | null;
	created_at: number;
	updated_at: number;
	committed_at: number | null;
	pushed_at: number | null;
}

export interface McpFactoryCiRun {
	ci_run_id: string;
	cycle_id: string;
	provider: string;
	repository: string;
	revision: string;
	external_run_id: string;
	check_id: string;
	status: string;
	conclusion: string | null;
	url: string | null;
	failure_summary: string | null;
	diagnosis_required: boolean;
	diagnosis_summary: string | null;
	created_at: number;
	updated_at: number;
	last_observed_at: number | null;
	diagnosed_at: number | null;
}

export interface McpFactoryProgress {
	percent: number;
	state: string;
	effective_state: string;
	phase_index: number;
	phase_count: number;
	outcome:
		| 'running'
		| 'recovering'
		| 'repairing'
		| 'paused'
		| 'approval_required'
		| 'success'
		| 'failed'
		| 'blocked'
		| 'cancelled';
	terminal: boolean;
	basis: 'server_state_machine';
	updated_at_ms: number;
}

export interface McpFactorySummary {
	cycle_count: number;
	current_cycle_ordinal: number;
	event_count: number;
	evidence_count: number;
	required_gates: number;
	passed_required_gates: number;
	failed_required_gates: number;
	active_workers: number;
	pending_approvals: number;
	reasoning_calls: number;
	input_tokens: number;
	output_tokens: number;
	reasoning_runtime_ms: number;
	reasoning_cost_microusd: number;
	last_event_sequence: number;
}

export interface McpFactoryRunDetail extends McpFactoryRunSummary {
	mission: string;
	acceptance_criteria: string[];
	model_id: string | null;
	resumable_state: string | null;
	policy: unknown;
	budget: unknown;
	cycle: McpFactoryCycle | null;
	cycles: McpFactoryCycle[];
	gates: McpFactoryGate[];
	gate_history: McpFactoryGate[];
	events: McpFactoryEvent[];
	evidence: McpFactoryEvidence[];
	workers: McpFactoryWorker[];
	reasoning: McpFactoryReasoning[];
	approvals: McpFactoryApproval[];
	metrics: McpFactoryMetric[];
	capability_outcomes: McpFactoryCapabilityOutcome[];
	commit_intents: McpFactoryCommitIntent[];
	ci_runs: McpFactoryCiRun[];
	progress: McpFactoryProgress;
	summary: McpFactorySummary;
}

export interface McpFactorySnapshot {
	version: 1;
	runs: McpFactoryRunSummary[];
	selected: McpFactoryRunDetail | null;
	fingerprint: string;
	generated_at_ms: number;
}

export interface McpFactoryStreamCallbacks {
	onSnapshot: (snapshot: McpFactorySnapshot) => void;
	onActivity?: (event: McpFactoryEvent) => void;
	onProgress?: (progress: McpFactoryProgress) => void;
	onOpen?: () => void;
	onError?: (error: unknown) => void;
}

export interface McpMemoryWorkspace {
	workspace_id: string;
	workspace_name: string;
	workspace_path: string;
}

export interface McpMemoryNode {
	id: string;
	label: string;
	kind: 'scope' | 'memory' | 'entity';
	scope: 'user' | 'workspace';
	workspace_id: string | null;
	workspace_name: string | null;
	path: string;
	heading: string;
	memory_id: string;
	preview: string;
	links?: string[];
	modified_at_ms: number;
	size: number;
	trust_level: string;
	confidence: number;
	status: string;
	source_layer?: 'scope' | 'canonical' | 'entity' | string;
	importance?: number;
	valid_from_ms?: number | null;
	valid_until_ms?: number | null;
	verified_at_ms?: number | null;
	verification_expires_at_ms?: number | null;
	verification_stale?: boolean;
	branch_id?: string | null;
	parent_memory_id?: string | null;
	superseded_by_id?: string | null;
	entity_type?: string;
	recall_count?: number;
	last_recalled_at_ms?: number;
}

export interface McpMemoryEdge {
	id: string;
	source: string;
	target: string;
	kind: 'belongs_to' | 'related';
	label?: string;
}

export interface McpMemoryEvent {
	event_id: string;
	workspace: string | null;
	event_type: 'recall' | 'write' | 'write_rejected' | string;
	scope: 'user' | 'workspace' | null;
	memory_id: string | null;
	path: string | null;
	heading: string | null;
	reason: string | null;
	trust_level: string;
	confidence: number;
	payload: Record<string, unknown>;
	created_at_ms: number;
}

export interface McpMemoryRecallItem {
	node_id: string;
	scope: 'user' | 'workspace';
	path: string;
	heading: string;
	memory_id: string;
	reason: string;
}

export interface McpMemoryRecallTrace {
	event_id: string;
	created_at_ms: number;
	workspace: string | null;
	context_chars: number;
	items: McpMemoryRecallItem[];
}

export interface McpMemoryNamespaceState {
	workspace: string;
	version: number;
	active_branch_id: string | null;
	active_snapshot_id: string | null;
	updated_at_ms: number;
}

export interface McpMemoryCheckpointState {
	checkpoint_id: string;
	workspace: string;
	task_key_hash: string;
	version: number;
	stage: string;
	memory_version: number;
	created_at_ms: number;
}

export interface McpMemorySnapshotState {
	snapshot_id: string;
	workspace: string;
	label: string;
	memory_version: number;
	record_count: number;
	created_at_ms: number;
}

export interface McpMemoryBranchState {
	branch_id: string;
	workspace: string;
	name: string;
	from_snapshot_id: string | null;
	status: string;
	created_at_ms: number;
	updated_at_ms: number;
}

export interface McpMemorySnapshot {
	version: 1 | 2 | 3;
	workspaces: McpMemoryWorkspace[];
	selected_workspace_id: string | null;
	nodes: McpMemoryNode[];
	edges: McpMemoryEdge[];
	events: McpMemoryEvent[];
	recall_traces: McpMemoryRecallTrace[];
	metrics: {
		memory_nodes?: number;
		managed_memory_nodes?: number;
		canonical_memory_nodes?: number;
		entity_nodes?: number;
		user_memory_nodes?: number;
		workspace_memory_nodes?: number;
		scope_nodes?: number;
		edge_count?: number;
		file_count?: number;
		total_bytes?: number;
		truncated?: boolean;
		recalls_24h?: number;
		writes_24h?: number;
		rejected_writes_24h?: number;
		event_count_visible?: number;
		superseded_memory_nodes?: number;
		stale_verification_nodes?: number;
		snapshot_count?: number;
		branch_count?: number;
		checkpoint_count?: number;
		memory_version?: number;
		pending_memory_jobs?: number;
		running_memory_jobs?: number;
		failed_memory_jobs?: number;
		vector_indexed_memories?: number;
		lexical_indexed_memories?: number;
		retrieval_learning_observations?: number;
		open_memory_conflicts?: number;
		procedure_profiles?: number;
		failure_profiles?: number;
	};
	health: {
		enabled?: boolean;
		tool_enabled?: boolean;
		background_review_enabled?: boolean;
		review_interval_turns?: number;
		canonical_store?: string;
		event_store?: string;
		retrieval?: string;
		realtime?: string;
		trust_policy?: string;
		required_for_execution?: boolean;
		context_char_limit?: number;
		verification_ttl_seconds?: number;
		maintenance_enabled?: boolean;
		maintenance_queue?: {
			pending?: number;
			running?: number;
			complete?: number;
			failed?: number;
		};
		advanced_status?: string;
		vector_index?: {
			backend?: string;
			model_id?: string;
			dimensions?: number;
			coverage?: number;
			hnsw?: boolean;
		};
		lexical_index?: {
			backend?: string;
			k1?: number;
			b?: number;
			coverage?: number;
		};
		retrieval_learning?: {
			weights?: Record<string, number>;
			observations?: number;
			updated_at_ms?: number;
		};
		open_conflicts?: number;
		intelligence?: {
			procedures?: number;
			failures?: number;
		};
		index_errors?: Record<string, { error_type?: string; at_ms?: number }>;
	};
	lifecycle?: {
		namespaces: McpMemoryNamespaceState[];
		checkpoints: McpMemoryCheckpointState[];
		snapshots: McpMemorySnapshotState[];
		branches: McpMemoryBranchState[];
	};
	fingerprint: string;
	generated_at_ms: number;
}

export interface McpMemoryTimelineRecord {
	memory_id: string;
	scope: string;
	kind: string;
	preview: string;
	status: string;
	trust_level: string;
	confidence: number;
	importance: number;
	valid_from_ms: number | null;
	valid_until_ms: number | null;
	observed_at_ms: number | null;
	superseded_at_ms: number | null;
	branch_id: string | null;
}

export interface McpMemoryTimeline {
	version: 2;
	workspace_id: string | null;
	at_ms: number;
	known_at_ms: number | null;
	records: McpMemoryTimelineRecord[];
	generated_at_ms: number;
}

export interface McpMemoryStreamCallbacks {
	onSnapshot: (snapshot: McpMemorySnapshot) => void;
	onOpen?: () => void;
	onError?: (error: unknown) => void;
}

export type McpDiagnosticsEvent = (
	McpLatencySample | McpFailureDiagnostic | McpBackendMetricsSample | McpUsageDiagnostic
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
	usage_periods?: McpUsagePeriods;
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

export const getMcpMemorySnapshot = (
	workspaceId?: string | null,
	nodeLimit = 400,
	eventLimit = 120
) => {
	const params = new URLSearchParams({
		node_limit: String(nodeLimit),
		event_limit: String(eventLimit)
	});
	if (workspaceId) params.set('workspace_id', workspaceId);
	return fetchJSON<McpMemorySnapshot>(`/api/mcp/memory/snapshot?${params.toString()}`);
};

export const getMcpMemoryTimeline = (
	atMs: number,
	workspaceId?: string | null,
	limit = 300,
	knownAtMs?: number | null
) => {
	const params = new URLSearchParams({
		at_ms: String(Math.max(0, Math.trunc(atMs))),
		limit: String(limit)
	});
	if (knownAtMs != null) params.set('known_at_ms', String(Math.max(0, Math.trunc(knownAtMs))));
	if (workspaceId) params.set('workspace_id', workspaceId);
	return fetchJSON<McpMemoryTimeline>(`/api/mcp/memory/timeline?${params.toString()}`);
};

export function openMcpMemoryStream(
	workspaceId: string | null | undefined,
	callbacks: McpMemoryStreamCallbacks,
	nodeLimit = 400,
	eventLimit = 120
): () => void {
	const params = new URLSearchParams({
		node_limit: String(nodeLimit),
		event_limit: String(eventLimit)
	});
	if (workspaceId) params.set('workspace_id', workspaceId);
	const source = new EventSource(`/api/mcp/memory/stream?${params.toString()}`);
	const parse = (message: MessageEvent<string>) => {
		try {
			callbacks.onSnapshot(JSON.parse(message.data) as McpMemorySnapshot);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) => parse(event as MessageEvent<string>));
	source.addEventListener('memory_error', (event) => {
		callbacks.onError?.(new Error((event as MessageEvent<string>).data || 'Memory stream failed'));
	});
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);
	return () => source.close();
}

export const getMcpFactorySnapshot = (runId?: string | null, runLimit = 20) => {
	const params = new URLSearchParams({ run_limit: String(runLimit) });
	if (runId) params.set('run_id', runId);
	return fetchJSON<McpFactorySnapshot>(`/api/mcp/factory/snapshot?${params.toString()}`);
};

export function openMcpFactoryStream(
	runId: string | null | undefined,
	callbacks: McpFactoryStreamCallbacks,
	runLimit = 20
): () => void {
	const params = new URLSearchParams({ run_limit: String(runLimit) });
	if (runId) params.set('run_id', runId);
	const source = new EventSource(`/api/mcp/factory/stream?${params.toString()}`);
	const parseSnapshot = (message: MessageEvent<string>) => {
		try {
			callbacks.onSnapshot(JSON.parse(message.data) as McpFactorySnapshot);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};
	const parseActivity = (message: MessageEvent<string>) => {
		try {
			callbacks.onActivity?.(JSON.parse(message.data) as McpFactoryEvent);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};
	const parseProgress = (message: MessageEvent<string>) => {
		try {
			callbacks.onProgress?.(JSON.parse(message.data) as McpFactoryProgress);
		} catch (error) {
			callbacks.onError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) => parseSnapshot(event as MessageEvent<string>));
	source.addEventListener('activity', (event) => parseActivity(event as MessageEvent<string>));
	source.addEventListener('progress', (event) => parseProgress(event as MessageEvent<string>));
	source.addEventListener('factory_error', (event) => {
		callbacks.onError?.(new Error((event as MessageEvent<string>).data || 'Factory stream failed'));
	});
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);
	return () => source.close();
}

export const getMcpTopologyConfig = () => fetchJSON<McpTopologyConfig>('/api/mcp/topology/config');

export const updateMcpTopologyConfig = (aliases: Record<string, string | null>) =>
	fetchJSON<McpTopologyConfig>('/api/mcp/topology/config', {
		...jsonBody({ aliases }),
		method: 'PUT'
	});

export const getMcpDiagnosticsSnapshot = () =>
	fetchJSON<McpDiagnosticsSnapshot>('/api/mcp/diagnostics/snapshot');

export const getMcpEngineeringSessions = (limit = 50) =>
	fetchJSON<McpEngineeringSessionsResponse>(`/api/mcp/engineering/sessions?limit=${limit}`);

export const getMcpBenchmarkLeaderboard = (suiteId = 'cptr-python-core') =>
	fetchJSON<McpBenchmarkLeaderboard>(
		`/api/mcp/benchmarks/leaderboard?suite_id=${encodeURIComponent(suiteId)}`
	);

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
