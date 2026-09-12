import type { WorkspaceListItem } from '$lib/apis/state';

export const WORKSPACE_ID_PARAM = 'workspaceId';
export const LEGACY_WORKSPACE_PATH_PARAM = 'workspace';
export const WORKSPACE_CENTER_ID_PARAM = 'workspaceCenterId';
export const WORKSPACE_CENTER_TAB_PARAM = 'workspaceCenter';
export const WORKSPACE_CENTER_TABS = [
	'overview',
	'general',
	'repositories',
	'groups',
	'instructions',
	'environment',
	'memory',
	'checkpoints',
	'tasks',
	'workbench',
	'admin',
	'activity',
	'health'
] as const;

export type WorkspaceCenterTab = (typeof WORKSPACE_CENTER_TABS)[number];

export function normalizeWorkspaceCenterTab(value: string | null | undefined): WorkspaceCenterTab {
	return WORKSPACE_CENTER_TABS.includes(value as WorkspaceCenterTab)
		? (value as WorkspaceCenterTab)
		: 'overview';
}

export interface WorkspaceRouteResolution {
	workspaceId: string | null;
	path: string | null;
	workspace: WorkspaceListItem | null;
	source: 'id' | 'path' | 'none';
	unresolvedId: string | null;
}

export function resolveWorkspaceRoute(
	params: URLSearchParams,
	workspaces: WorkspaceListItem[]
): WorkspaceRouteResolution {
	const workspaceId = params.get(WORKSPACE_ID_PARAM)?.trim() || null;
	if (workspaceId) {
		const workspace = workspaces.find((item) => item.workspace_id === workspaceId) ?? null;
		return {
			workspaceId,
			path: workspace?.path ?? null,
			workspace,
			source: 'id',
			unresolvedId: workspace ? null : workspaceId
		};
	}

	const path = params.get(LEGACY_WORKSPACE_PATH_PARAM)?.trim() || null;
	if (path) {
		const workspace = workspaces.find((item) => item.path === path) ?? null;
		return {
			workspaceId: workspace?.workspace_id ?? null,
			path,
			workspace,
			source: 'path',
			unresolvedId: null
		};
	}

	return {
		workspaceId: null,
		path: null,
		workspace: null,
		source: 'none',
		unresolvedId: null
	};
}

export function setInternalWorkspaceRoute(
	params: URLSearchParams,
	workspace: Pick<WorkspaceListItem, 'workspace_id'>
): URLSearchParams {
	const next = new URLSearchParams(params);
	next.set(WORKSPACE_ID_PARAM, workspace.workspace_id);
	next.delete(LEGACY_WORKSPACE_PATH_PARAM);
	return next;
}

export function setWorkspaceRouteForPath(
	params: URLSearchParams,
	path: string,
	workspaces: WorkspaceListItem[]
): URLSearchParams {
	const workspace = workspaces.find((item) => item.path === path);
	if (workspace?.workspace_id) return setInternalWorkspaceRoute(params, workspace);
	const next = new URLSearchParams(params);
	next.set(LEGACY_WORKSPACE_PATH_PARAM, path);
	next.delete(WORKSPACE_ID_PARAM);
	return next;
}

export function clearWorkspaceRoute(params: URLSearchParams): URLSearchParams {
	const next = new URLSearchParams(params);
	next.delete(WORKSPACE_ID_PARAM);
	next.delete(LEGACY_WORKSPACE_PATH_PARAM);
	return next;
}

export function setWorkspaceCenterRoute(
	params: URLSearchParams,
	workspaceId: string,
	tab = 'overview'
): URLSearchParams {
	const next = new URLSearchParams(params);
	next.set(WORKSPACE_CENTER_ID_PARAM, workspaceId);
	next.set(WORKSPACE_CENTER_TAB_PARAM, tab);
	return next;
}

export function clearWorkspaceCenterRoute(params: URLSearchParams): URLSearchParams {
	const next = new URLSearchParams(params);
	next.delete(WORKSPACE_CENTER_ID_PARAM);
	next.delete(WORKSPACE_CENTER_TAB_PARAM);
	return next;
}
