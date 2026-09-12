import { fetchJSON, jsonBody } from '$lib/apis';

const BASE = '/api/control/v1/workbench-sessions';

export interface WorkbenchSessionView {
	session_id: string;
	name: string;
	workspace_id: string | null;
	status: string;
	active_target_type: 'task' | 'command' | 'monitor' | null;
	active_target_id: string | null;
	active_workspace_id: string | null;
	event_count: number;
	created_at: number;
	updated_at: number;
	last_event_at: number | null;
	archived_at: number | null;
	environment_profile_id: string | null;
	environment_profile_override: Record<string, unknown> | null;
	admin_role: string | null;
	role_context: Record<string, unknown> | null;
	last_context_snapshot_id: string | null;
}

export interface WorkbenchSessionEventView {
	event_id: string;
	session_id: string;
	sequence: number;
	source: string;
	actor: string;
	event_type: string;
	state: string | null;
	target_type: 'task' | 'command' | 'monitor' | null;
	target_id: string | null;
	workspace_id: string | null;
	tool_name: string | null;
	summary: string;
	details: Record<string, unknown>;
	metrics: Record<string, unknown>;
	policy: Record<string, unknown>;
	created_at: number;
}

export interface WorkbenchAdminGrantStatus {
	active: boolean;
	grant_id: string | null;
	expires_at: number | null;
	remaining_seconds: number;
}

export interface WorkbenchPrivilegeView {
	session_id: string;
	privilege: 'NORMAL' | 'ADMIN' | 'ROOT' | string;
	admin_grant: WorkbenchAdminGrantStatus;
	local_root_grant_active: boolean;
}

export const listWorkbenchSessions = (includeArchived = false, limit = 100) =>
	fetchJSON<{ sessions: WorkbenchSessionView[] }>(
		BASE +
			'?limit=' +
			encodeURIComponent(String(limit)) +
			'&include_archived=' +
			(includeArchived ? 'true' : 'false')
	);

export const getWorkbenchSession = (sessionId: string) =>
	fetchJSON<WorkbenchSessionView>(BASE + '/' + encodeURIComponent(sessionId));

export const getWorkbenchSessionEvents = (sessionId: string, afterSequence = 0, limit = 200) =>
	fetchJSON<{
		session_id: string;
		events: WorkbenchSessionEventView[];
		last_sequence: number;
	}>(
		BASE +
			'/' +
			encodeURIComponent(sessionId) +
			'/events?after_sequence=' +
			encodeURIComponent(String(afterSequence)) +
			'&limit=' +
			encodeURIComponent(String(limit))
	);

export const renameWorkbenchSession = (sessionId: string, name: string) =>
	fetchJSON<WorkbenchSessionView>(BASE + '/' + encodeURIComponent(sessionId), {
		...jsonBody({ name }),
		method: 'PATCH'
	});

export const archiveWorkbenchSession = (sessionId: string) =>
	fetchJSON<WorkbenchSessionView>(BASE + '/' + encodeURIComponent(sessionId) + '/archive', {
		method: 'POST'
	});

export const getWorkbenchPrivilege = (sessionId: string) =>
	fetchJSON<WorkbenchPrivilegeView>(BASE + '/' + encodeURIComponent(sessionId) + '/privilege');

export const grantWorkbenchAdmin = (sessionId: string, ttlSeconds: number) =>
	fetchJSON<WorkbenchAdminGrantStatus>(
		BASE + '/' + encodeURIComponent(sessionId) + '/admin-grant',
		jsonBody({ ttl_seconds: ttlSeconds })
	);

export const revokeWorkbenchAdmin = (sessionId: string) =>
	fetchJSON<{ session_id: string; revoked: boolean; revoked_count: number }>(
		BASE + '/' + encodeURIComponent(sessionId) + '/admin-revoke',
		{ method: 'POST' }
	);
