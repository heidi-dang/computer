import type {
	McpActivityEvent,
	McpActivityPhase,
	McpActivitySnapshot,
	McpContentItem
} from '$lib/apis/mcp';

export type McpActivitySource = 'plugin' | 'console';

export type McpActivityRow = {
	id: string;
	correlationKey: string;
	source: McpActivitySource;
	sequence: number;
	clientId: string | null;
	clientLabel: string;
	clientVersion: string | null;
	toolName: string;
	title: string | null;
	phase: McpActivityPhase;
	summary: string;
	startedAt: number;
	completedAt: number | null;
	durationMs: number | null;
	argumentsJson: string | null;
	resultJson: string | null;
	errorJson: string | null;
	requestId: string | null;
	sessionId: string | null;
	contentItems?: McpContentItem[];
};

export type McpActivityState = {
	sequence: number;
	eventCapacity: number;
	rows: McpActivityRow[];
	seenEventIds: string[];
};

function boundedTail<T>(items: T[], limit: number): T[] {
	return items.length <= limit ? items : items.slice(items.length - limit);
}

function correlationKey(event: McpActivityEvent): string {
	return event.request_id
		? `plugin:${event.request_id}:${event.tool_name}`
		: `plugin:event:${event.event_id}`;
}

function startedAt(event: McpActivityEvent): number {
	return event.phase === 'started'
		? event.timestamp_ms
		: Math.max(0, event.timestamp_ms - (event.duration_ms ?? 0));
}

function rowFromEvent(event: McpActivityEvent, current?: McpActivityRow): McpActivityRow {
	const terminal = event.phase !== 'started';
	return {
		id: current?.id ?? event.event_id,
		correlationKey: current?.correlationKey ?? correlationKey(event),
		source: 'plugin',
		sequence: event.ingestion_sequence,
		clientId: event.client.id,
		clientLabel: event.client.label,
		clientVersion: event.client.version,
		toolName: event.tool_name,
		title: event.title,
		phase: event.phase,
		summary: event.summary,
		startedAt: current?.startedAt ?? startedAt(event),
		completedAt: terminal ? event.timestamp_ms : null,
		durationMs: terminal ? event.duration_ms : null,
		argumentsJson: event.arguments_json ?? current?.argumentsJson ?? null,
		resultJson: event.result_json ?? (terminal ? null : (current?.resultJson ?? null)),
		errorJson: event.error_json ?? (terminal ? null : (current?.errorJson ?? null)),
		requestId: event.request_id ?? current?.requestId ?? null,
		sessionId: event.session_id ?? current?.sessionId ?? null,
		contentItems: current?.contentItems
	};
}

function applyFreshEvent(state: McpActivityState, event: McpActivityEvent): McpActivityState {
	const key = correlationKey(event);
	const rows = state.rows.map((row) => ({ ...row }));
	const index = rows.findIndex((row) => row.source === 'plugin' && row.correlationKey === key);
	const current = index >= 0 ? rows[index] : undefined;
	const row = rowFromEvent(event, current);
	if (index >= 0) rows[index] = row;
	else rows.push(row);

	return {
		...state,
		sequence: event.ingestion_sequence,
		rows: boundedTail(rows, state.eventCapacity),
		seenEventIds: boundedTail([...state.seenEventIds, event.event_id], state.eventCapacity * 2)
	};
}

export function hydrateMcpActivity(snapshot: McpActivitySnapshot): McpActivityState {
	const eventCapacity = Math.max(
		1,
		snapshot.stream_health.event_capacity || snapshot.events.length || 1
	);
	let state: McpActivityState = {
		sequence: 0,
		eventCapacity,
		rows: [],
		seenEventIds: []
	};
	for (const event of snapshot.events) {
		if (event.ingestion_sequence <= state.sequence || state.seenEventIds.includes(event.event_id))
			continue;
		state = applyFreshEvent(state, event);
	}
	return { ...state, sequence: Math.max(state.sequence, snapshot.sequence) };
}

export function applyMcpActivityEvent(
	state: McpActivityState,
	event: McpActivityEvent
): McpActivityState {
	if (event.ingestion_sequence <= state.sequence || state.seenEventIds.includes(event.event_id)) {
		return state;
	}
	return applyFreshEvent(state, event);
}

export function mergeConsoleActivityRow(
	state: McpActivityState,
	row: McpActivityRow
): McpActivityState {
	const rows = state.rows.map((item) => ({ ...item }));
	const index = rows.findIndex((item) => item.source === 'console' && item.id === row.id);
	if (index >= 0) rows[index] = { ...row, source: 'console' };
	else rows.push({ ...row, source: 'console' });
	return { ...state, rows: boundedTail(rows, state.eventCapacity) };
}
