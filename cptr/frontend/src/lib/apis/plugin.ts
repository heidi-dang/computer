import { fetchJSON, jsonBody } from '$lib/apis';

export type WorkbenchTargetType = 'task' | 'command' | 'monitor';

export interface WorkbenchSession {
	session_id: string;
	name: string;
	status: 'OPEN' | 'ARCHIVED' | 'DELETE_PENDING' | string;
	workspace_id: string | null;
	active_target_type: WorkbenchTargetType | null;
	active_target_id: string | null;
	active_workspace_id: string | null;
	created_at: string | number;
	updated_at: string | number;
	event_count: number;
	last_event_at: string | number | null;
	archived_at: string | number | null;
}

export interface WorkbenchSessionEvent {
	session_id: string;
	sequence: number;
	source: 'plugin' | 'workbench' | 'system' | string;
	actor: string;
	event_type: string;
	state: string | null;
	target_type: WorkbenchTargetType | null;
	target_id: string | null;
	workspace_id: string | null;
	tool_name: string | null;
	summary: string;
	details: Record<string, unknown>;
	metrics: Record<string, unknown>;
	policy: Record<string, unknown>;
	created_at: string | number;
}

export interface WorkbenchDeleteRequest {
	confirmation_id: string;
	expires_at: string | number;
	session_id: string;
}

export interface PluginTerminalSnapshot {
	target: WorkbenchTargetType;
	snapshot: {
		id?: string;
		monitor_id?: string;
		command_id?: string;
		workspace_id?: string;
		status?: string;
		exit_code?: number | null;
		error?: string | null;
		created_at?: string | number;
		updated_at?: string | number;
		current_scope?: string | null;
	};
}

export interface PluginTerminalEvent {
	sequence: number;
	event_type: string;
	payload: Record<string, unknown>;
	created_at?: string | number;
}

const query = (afterSequence = 0) => `?after_sequence=${Math.max(0, afterSequence)}`;

export const listPluginSessions = (limit = 50, includeArchived = false) =>
	fetchJSON<{ sessions: WorkbenchSession[] }>(
		`/api/plugin/v1/sessions?limit=${limit}&include_archived=${includeArchived}`
	);

export const getPluginSession = (sessionId: string) =>
	fetchJSON<WorkbenchSession>(`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}`);

export const getPluginSessionEvents = (sessionId: string, afterSequence = 0, limit = 100) =>
	fetchJSON<{ session_id: string; events: WorkbenchSessionEvent[] }>(
		`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/events?after_sequence=${Math.max(0, afterSequence)}&limit=${limit}`
	);

export const renamePluginSession = (sessionId: string, name: string) =>
	fetchJSON<WorkbenchSession>(
		`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/rename`,
		jsonBody({ name })
	);

export const archivePluginSession = (sessionId: string) =>
	fetchJSON<WorkbenchSession>(
		`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/archive`,
		jsonBody({})
	);

export const requestPluginSessionDelete = (sessionId: string) =>
	fetchJSON<WorkbenchDeleteRequest>(
		`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/delete-request`,
		jsonBody({})
	);

export const confirmPluginSessionDelete = (confirmationId: string) =>
	fetchJSON<{ deleted: boolean; session_id: string }>(
		'/api/plugin/v1/sessions/delete-confirm',
		jsonBody({ confirmation_id: confirmationId })
	);

export const pluginSessionStreamUrl = (sessionId: string, afterSequence = 0) =>
	`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/stream${query(afterSequence)}`;

export const pluginTerminalStreamUrl = (sessionId: string, afterSequence = 0) =>
	`/api/plugin/v1/sessions/${encodeURIComponent(sessionId)}/terminal/stream${query(afterSequence)}`;


export interface WorkspaceMemoryFact {
	fact_id: string;
	category: string;
	content: string;
	paths: string[];
	source_event_id: string | null;
	status: 'ACTIVE' | 'STALE' | 'ARCHIVED' | string;
	pinned: boolean;
	revision: number;
	verified_fingerprint: string | null;
	created_at: string | number;
	updated_at: string | number;
}

export interface WorkspaceMemoryEvent {
	event_id: string;
	sequence: number;
	kind: string;
	source: string;
	session_id: string | null;
	tool_name: string | null;
	outcome: string;
	summary: string;
	affected_paths: string[];
	details: Record<string, unknown>;
	workspace_fingerprint: string | null;
	created_at: string | number;
}

export interface WorkspaceMemoryContext {
	workspace_id: string;
	memory_cursor: number;
	workspace_stage: Record<string, unknown>;
	relevant_facts: WorkspaceMemoryFact[];
	freshness: Record<string, unknown>;
}

export const getPluginWorkspaceMemoryContext = (workspaceId: string, refresh = false) =>
	fetchJSON<WorkspaceMemoryContext>(
		`/api/plugin/v1/workspace-memory/workspaces/${encodeURIComponent(workspaceId)}/context${
			refresh ? '?refresh=true' : ''
		}`
	);

export const getPluginWorkspaceMemoryFacts = (workspaceId: string, includeStale = true) =>
	fetchJSON<{ workspace_id: string; facts: WorkspaceMemoryFact[] }>(
		`/api/plugin/v1/workspace-memory/workspaces/${encodeURIComponent(workspaceId)}/facts${
			includeStale ? '' : '?include_stale=false'
		}`
	);

export const updatePluginWorkspaceMemoryFact = (
	workspaceId: string,
	factId: string,
	body: { content?: string; pinned?: boolean; status?: string }
) =>
	fetchJSON<WorkspaceMemoryFact>(
		`/api/plugin/v1/workspace-memory/workspaces/${encodeURIComponent(workspaceId)}/facts/${encodeURIComponent(factId)}`,
		{ method: 'PATCH', ...jsonBody(body) }
	);

export const forgetPluginWorkspaceMemoryFact = (workspaceId: string, factId: string) =>
	fetchJSON<{ workspace_id: string; fact_id: string; forgotten_at: string | number }>(
		`/api/plugin/v1/workspace-memory/workspaces/${encodeURIComponent(workspaceId)}/facts/${encodeURIComponent(factId)}`,
		{ method: 'DELETE' }
	);

export const clearPluginWorkspaceMemory = (workspaceId: string) =>
	fetchJSON<{ workspace_id: string; cleared_at: string | number; cursor: number }>(
		'/api/plugin/v1/workspace-memory/clear',
		jsonBody({ workspace_id: workspaceId, confirm: true })
	);
