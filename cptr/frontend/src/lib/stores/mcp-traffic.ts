import type {
	McpTrafficClient,
	McpTrafficClientSnapshot,
	McpTrafficErrorCode,
	McpTrafficEvent,
	McpTrafficSessionSnapshot,
	McpTrafficSnapshot
} from '$lib/apis/mcp';

export type McpTrafficClientState = {
	id: string;
	label: string;
	version: string | null;
	activeSessions: number;
	activeRequests: number;
	totalRequests: number;
	errors: number;
	lastSeen: number;
	lastTool: string | null;
};

export type McpTrafficSessionState = {
	sessionId: string;
	clientId: string;
	connectedAt: number;
	lastSeen: number;
};

export type McpTrafficActiveRequest = {
	requestId: string;
	correlationId: string | null;
	clientId: string;
	startedAt: number;
	method: string | null;
	toolName: string | null;
	sessionId: string | null;
	requestBytes: number | null;
};

export type McpRecentRequestRow = {
	requestId: string;
	correlationId: string | null;
	clientId: string;
	clientLabel: string;
	clientVersion: string | null;
	method: string | null;
	toolName: string | null;
	status: 'active' | 'complete' | 'error';
	startedAt: number;
	completedAt: number | null;
	durationMs: number | null;
	requestBytes: number | null;
	responseBytes: number | null;
	errorCode: McpTrafficErrorCode | null;
	sessionId: string | null;
};

export type McpTrafficState = {
	sequence: number;
	eventCapacity: number;
	sessionCapacity: number;
	clientCapacity: number;
	events: McpTrafficEvent[];
	clients: Record<string, McpTrafficClientState>;
	sessions: Record<string, McpTrafficSessionState>;
	activeRequests: Record<string, McpTrafficActiveRequest>;
	recentRequests: McpRecentRequestRow[];
	seenEventIds: string[];
};

export type McpTopologyNode = McpTrafficClientState & {
	x: number;
	y: number;
	angle: number;
	connected: boolean;
	active: boolean;
};

export type McpRequestOutcomeTotals = {
	total: number;
	success: number;
	failed: number;
	active: number;
};

export type McpRequestTimelineBucket = {
	startMs: number;
	endMs: number;
	success: number;
	failed: number;
	total: number;
};

function clientFromSnapshot(client: McpTrafficClientSnapshot): McpTrafficClientState {
	return {
		id: client.id,
		label: client.label,
		version: client.version,
		activeSessions: client.active_sessions,
		activeRequests: client.active_requests,
		totalRequests: client.total_requests,
		errors: client.errors,
		lastSeen: client.last_seen,
		lastTool: client.last_tool
	};
}

function sessionFromSnapshot(session: McpTrafficSessionSnapshot): McpTrafficSessionState {
	return {
		sessionId: session.session_id,
		clientId: session.client_id,
		connectedAt: session.connected_at,
		lastSeen: session.last_seen
	};
}

function defaultClient(client: McpTrafficClient, timestamp: number): McpTrafficClientState {
	return {
		id: client.id,
		label: client.label,
		version: client.version,
		activeSessions: 0,
		activeRequests: 0,
		totalRequests: 0,
		errors: 0,
		lastSeen: timestamp,
		lastTool: null
	};
}

function eventLimit(snapshot: McpTrafficSnapshot): number {
	return Math.max(1, snapshot.stream_health.event_capacity || snapshot.events.length || 1);
}

function boundedTail<T>(items: T[], limit: number): T[] {
	return items.length <= limit ? items : items.slice(items.length - limit);
}

function oldestKey<T extends { lastSeen?: number; startedAt?: number }>(
	record: Record<string, T>
): string | null {
	return (
		Object.entries(record).sort(
			([leftId, left], [rightId, right]) =>
				(left.lastSeen ?? left.startedAt ?? 0) - (right.lastSeen ?? right.startedAt ?? 0) ||
				leftId.localeCompare(rightId)
		)[0]?.[0] ?? null
	);
}

function pruneClients(clients: Record<string, McpTrafficClientState>, capacity: number): void {
	while (Object.keys(clients).length > capacity) {
		const removable = Object.values(clients)
			.filter((client) => client.activeSessions === 0 && client.activeRequests === 0)
			.sort((a, b) => a.lastSeen - b.lastSeen || a.id.localeCompare(b.id))[0];
		if (!removable) break;
		delete clients[removable.id];
	}
}

function upsertClient(
	clients: Record<string, McpTrafficClientState>,
	event: McpTrafficEvent
): McpTrafficClientState {
	const current = clients[event.client.id] ?? defaultClient(event.client, event.timestamp_ms);
	const next: McpTrafficClientState = {
		...current,
		label: event.client.label,
		version: event.client.version,
		lastSeen: Math.max(current.lastSeen, event.timestamp_ms),
		lastTool: event.tool_name ?? current.lastTool
	};
	clients[event.client.id] = next;
	return next;
}

function activeRequestFrom(event: McpTrafficEvent): McpTrafficActiveRequest | null {
	if (!event.request_id) return null;
	return {
		requestId: event.request_id,
		correlationId: event.correlation_id,
		clientId: event.client.id,
		startedAt: event.timestamp_ms,
		method: event.method,
		toolName: event.tool_name,
		sessionId: event.session_id,
		requestBytes: event.request_bytes
	};
}

function activeRow(
	request: McpTrafficActiveRequest,
	client: McpTrafficClientState
): McpRecentRequestRow {
	return {
		requestId: request.requestId,
		correlationId: request.correlationId,
		clientId: request.clientId,
		clientLabel: client.label,
		clientVersion: client.version,
		method: request.method,
		toolName: request.toolName,
		status: 'active',
		startedAt: request.startedAt,
		completedAt: null,
		durationMs: null,
		requestBytes: request.requestBytes,
		responseBytes: null,
		errorCode: null,
		sessionId: request.sessionId
	};
}

function terminalRow(
	event: McpTrafficEvent,
	client: McpTrafficClientState,
	active?: McpTrafficActiveRequest
): McpRecentRequestRow | null {
	if (!event.request_id) return null;
	const startedAt = active?.startedAt ?? Math.max(0, event.timestamp_ms - (event.duration_ms ?? 0));
	return {
		requestId: event.request_id,
		correlationId: event.correlation_id ?? active?.correlationId ?? null,
		clientId: event.client.id,
		clientLabel: client.label,
		clientVersion: client.version,
		method: event.method ?? active?.method ?? null,
		toolName: event.tool_name ?? active?.toolName ?? null,
		status: event.event_type === 'request_failed' ? 'error' : 'complete',
		startedAt,
		completedAt: event.timestamp_ms,
		durationMs: event.duration_ms,
		requestBytes: event.request_bytes ?? active?.requestBytes ?? null,
		responseBytes: event.response_bytes,
		errorCode: event.error_code,
		sessionId: event.session_id ?? active?.sessionId ?? null
	};
}

function replaceRecentRow(
	rows: McpRecentRequestRow[],
	row: McpRecentRequestRow,
	limit: number
): McpRecentRequestRow[] {
	return boundedTail([...rows.filter((item) => item.requestId !== row.requestId), row], limit);
}

function reconstructDerivedState(
	events: McpTrafficEvent[],
	clients: Record<string, McpTrafficClientState>,
	limit: number
): {
	activeRequests: Record<string, McpTrafficActiveRequest>;
	recentRequests: McpRecentRequestRow[];
} {
	const activeRequests: Record<string, McpTrafficActiveRequest> = {};
	let recentRequests: McpRecentRequestRow[] = [];

	for (const event of events) {
		const client = clients[event.client.id] ?? defaultClient(event.client, event.timestamp_ms);
		clients[event.client.id] = client;
		if (event.event_type === 'request_started') {
			const request = activeRequestFrom(event);
			if (!request) continue;
			activeRequests[request.requestId] = request;
			recentRequests = replaceRecentRow(recentRequests, activeRow(request, client), limit);
		} else if (event.event_type === 'request_finished' || event.event_type === 'request_failed') {
			const active = event.request_id ? activeRequests[event.request_id] : undefined;
			const row = terminalRow(event, client, active);
			if (event.request_id) delete activeRequests[event.request_id];
			if (row) recentRequests = replaceRecentRow(recentRequests, row, limit);
		}
	}

	return { activeRequests, recentRequests };
}

export function hydrateMcpTraffic(snapshot: McpTrafficSnapshot): McpTrafficState {
	const limit = eventLimit(snapshot);
	const events = boundedTail([...snapshot.events], limit);
	const clients = Object.fromEntries(
		snapshot.clients.map((client) => [client.id, clientFromSnapshot(client)])
	) as Record<string, McpTrafficClientState>;
	const sessions = Object.fromEntries(
		snapshot.sessions.map((session) => [session.session_id, sessionFromSnapshot(session)])
	) as Record<string, McpTrafficSessionState>;
	const derived = reconstructDerivedState(events, clients, limit);

	const sessionCapacity = Math.max(
		1,
		snapshot.stream_health.session_capacity || snapshot.sessions.length || 1
	);
	const clientCapacity = Math.max(8, limit + sessionCapacity);
	pruneClients(clients, clientCapacity);

	return {
		sequence: snapshot.sequence,
		eventCapacity: limit,
		sessionCapacity,
		clientCapacity,
		events,
		clients,
		sessions,
		activeRequests: derived.activeRequests,
		recentRequests: derived.recentRequests,
		seenEventIds: boundedTail(
			events.map((event) => event.event_id),
			limit * 2
		)
	};
}

export function applyMcpTrafficEvent(
	state: McpTrafficState,
	event: McpTrafficEvent
): McpTrafficState {
	if (event.ingestion_sequence <= state.sequence || state.seenEventIds.includes(event.event_id)) {
		return state;
	}

	const clients = Object.fromEntries(
		Object.entries(state.clients).map(([id, client]) => [id, { ...client }])
	) as Record<string, McpTrafficClientState>;
	const sessions = Object.fromEntries(
		Object.entries(state.sessions).map(([id, session]) => [id, { ...session }])
	) as Record<string, McpTrafficSessionState>;
	const activeRequests = Object.fromEntries(
		Object.entries(state.activeRequests).map(([id, request]) => [id, { ...request }])
	) as Record<string, McpTrafficActiveRequest>;
	let recentRequests = state.recentRequests.map((row) => ({ ...row }));
	const client = upsertClient(clients, event);

	if (event.session_id && sessions[event.session_id]) {
		sessions[event.session_id] = {
			...sessions[event.session_id],
			lastSeen: Math.max(sessions[event.session_id].lastSeen, event.timestamp_ms)
		};
	}

	switch (event.event_type) {
		case 'session_opened':
			if (event.session_id) {
				if (!sessions[event.session_id] && Object.keys(sessions).length >= state.sessionCapacity) {
					const evictedSessionId = oldestKey(sessions);
					if (evictedSessionId) {
						const evictedClient = clients[sessions[evictedSessionId].clientId];
						if (evictedClient) {
							evictedClient.activeSessions = Math.max(0, evictedClient.activeSessions - 1);
						}
						delete sessions[evictedSessionId];
					}
				}
				if (!sessions[event.session_id]) client.activeSessions += 1;
				sessions[event.session_id] = {
					sessionId: event.session_id,
					clientId: event.client.id,
					connectedAt: event.timestamp_ms,
					lastSeen: event.timestamp_ms
				};
			}
			break;
		case 'session_closed':
			if (event.session_id && sessions[event.session_id]) {
				const sessionClientId = sessions[event.session_id].clientId;
				delete sessions[event.session_id];
				const sessionClient = clients[sessionClientId];
				if (sessionClient)
					sessionClient.activeSessions = Math.max(0, sessionClient.activeSessions - 1);
			}
			break;
		case 'request_started': {
			const request = activeRequestFrom(event);
			if (request && !activeRequests[request.requestId]) {
				if (Object.keys(activeRequests).length >= state.eventCapacity) {
					const evictedRequestId = oldestKey(activeRequests);
					if (evictedRequestId) {
						const evictedClient = clients[activeRequests[evictedRequestId].clientId];
						if (evictedClient) {
							evictedClient.activeRequests = Math.max(0, evictedClient.activeRequests - 1);
						}
						delete activeRequests[evictedRequestId];
						recentRequests = recentRequests.filter((row) => row.requestId !== evictedRequestId);
					}
				}
				activeRequests[request.requestId] = request;
				client.activeRequests += 1;
				recentRequests = replaceRecentRow(
					recentRequests,
					activeRow(request, client),
					state.eventCapacity
				);
			}
			break;
		}
		case 'request_finished':
		case 'request_failed': {
			const active = event.request_id ? activeRequests[event.request_id] : undefined;
			if (event.request_id && active) delete activeRequests[event.request_id];
			client.activeRequests = Math.max(0, client.activeRequests - 1);
			client.totalRequests += 1;
			if (event.event_type === 'request_failed') client.errors += 1;
			const row = terminalRow(event, client, active);
			if (row) recentRequests = replaceRecentRow(recentRequests, row, state.eventCapacity);
			break;
		}
		case 'tool_started':
		case 'tool_finished':
		case 'tool_failed':
			if (event.tool_name) client.lastTool = event.tool_name;
			break;
	}

	pruneClients(clients, state.clientCapacity);

	return {
		...state,
		sequence: event.ingestion_sequence,
		events: boundedTail([...state.events, event], state.eventCapacity),
		clients,
		sessions,
		activeRequests,
		recentRequests,
		seenEventIds: boundedTail([...state.seenEventIds, event.event_id], state.eventCapacity * 2)
	};
}

export function requestOutcomeTotals(state: McpTrafficState): McpRequestOutcomeTotals {
	const total = Object.values(state.clients).reduce((sum, client) => sum + client.totalRequests, 0);
	const failed = Object.values(state.clients).reduce((sum, client) => sum + client.errors, 0);
	return {
		total,
		success: Math.max(0, total - failed),
		failed,
		active: Object.keys(state.activeRequests).length
	};
}

export function requestTimeline(
	state: McpTrafficState,
	nowMs: number,
	options: { windowMs?: number; bucketMs?: number } = {}
): McpRequestTimelineBucket[] {
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
	const buckets: McpRequestTimelineBucket[] = Array.from({ length: bucketCount }, (_, index) => ({
		startMs: startMs + index * bucketMs,
		endMs: startMs + (index + 1) * bucketMs,
		success: 0,
		failed: 0,
		total: 0
	}));

	for (const event of state.events) {
		if (event.event_type !== 'request_finished' && event.event_type !== 'request_failed') continue;
		if (event.timestamp_ms < startMs || event.timestamp_ms > endMs) continue;
		const rawIndex = Math.floor((event.timestamp_ms - startMs) / bucketMs);
		const index = Math.min(bucketCount - 1, Math.max(0, rawIndex));
		const bucket = buckets[index];
		bucket.total += 1;
		if (event.event_type === 'request_failed') bucket.failed += 1;
		else bucket.success += 1;
	}

	return buckets;
}

export function topologyNodes(state: McpTrafficState): McpTopologyNode[] {
	const clients = Object.values(state.clients).sort((a, b) => a.id.localeCompare(b.id));
	const count = clients.length;
	if (count === 0) return [];

	return clients.map((client, index) => {
		const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
		return {
			...client,
			x: 0.5 + Math.cos(angle) * 0.38,
			y: 0.5 + Math.sin(angle) * 0.34,
			angle,
			connected: client.activeSessions > 0,
			active: client.activeRequests > 0
		};
	});
}

export function recentRequestRows(state: McpTrafficState): McpRecentRequestRow[] {
	return [...state.recentRequests].sort(
		(a, b) => (b.completedAt ?? b.startedAt) - (a.completedAt ?? a.startedAt)
	);
}
