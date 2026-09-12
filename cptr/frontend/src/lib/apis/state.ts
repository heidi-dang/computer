/**
 * State API: user preferences, workspace state, welcome/system info.
 *
 * State is split into three layers:
 *   - preferences: global user prefs (theme, locale, etc.)
 *   - workspaces: per-workspace state keyed by filesystem path
 *   - active workspace: determined by URL query param, not stored server-side
 */
import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

// ── Preferences ─────────────────────────────────────────────────

export const getPreferences = () => fetchJSON<Record<string, unknown>>('/api/state/preferences');

export const savePreferences = (data: Record<string, unknown>) =>
	fetchHandler('/api/state/preferences', { ...jsonBody(data), method: 'PUT' });

// ── Workspace list (sidebar) ────────────────────────────────────

export interface WorkspaceListItem {
	workspace_id: string;
	path: string;
	name: string;
	slug: string | null;
	workspace_type: string;
	unread_count: number;
}

export const getWorkspaceList = () => fetchJSON<WorkspaceListItem[]>('/api/state/workspaces');

// ── Single workspace CRUD ───────────────────────────────────────

export type WorkspaceStateResponse = Record<string, unknown> & {
	workspace_id?: string;
	path?: string;
	name?: string;
	slug?: string | null;
	workspace_type?: string;
};

export type WorkspaceSaveResponse =
	| {
			status: 'saved';
			workspace_id: string;
			slug: string | null;
			workspace_type: string;
			path: string;
	  }
	| {
			status: 'skipped';
	  };

export const getWorkspaceState = (path: string) =>
	fetchJSON<WorkspaceStateResponse>(`/api/state/workspace?path=${encodeURIComponent(path)}`);

export const saveWorkspaceState = (path: string, data: Record<string, unknown>) =>
	fetchJSON<WorkspaceSaveResponse>(`/api/state/workspace?path=${encodeURIComponent(path)}`, {
		...jsonBody(data),
		method: 'PUT'
	});

export const deleteWorkspace = (path: string) =>
	fetchHandler(`/api/state/workspace?path=${encodeURIComponent(path)}`, { method: 'DELETE' });

// ── Welcome page ────────────────────────────────────────────────

export interface WelcomeSuggestion {
	name: string;
	path: string;
}

export interface WelcomeResponse {
	hostname: string;
	platform: string;
	version: string;
	system: unknown;
	processes: unknown[];
	suggestions: WelcomeSuggestion[];
}

export const getWelcome = () => fetchJSON<WelcomeResponse>('/api/state/welcome');
