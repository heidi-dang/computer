import { fetchJSON, jsonBody } from '$lib/apis';

export type WorkspaceOsAction =
	| 'resolve'
	| 'context'
	| 'health'
	| 'groups'
	| 'reconcile'
	| 'workspace_update'
	| 'repositories'
	| 'repository_catalog'
	| 'repository_add'
	| 'repository_update'
	| 'repository_remove'
	| 'instructions'
	| 'instruction_history'
	| 'instruction_preview'
	| 'instruction_save'
	| 'environment_profiles'
	| 'environment_create'
	| 'environment_version_create'
	| 'environment_set_active_version'
	| 'environment_set_target'
	| 'checkpoints';

export interface WorkspaceSummary {
	workspace_id: string;
	name: string;
	slug: string | null;
	workspace_type: string;
	available?: boolean;
	last_used_at?: number | null;
}

export interface WorkspaceRepositorySummary {
	repository_id: string;
	name: string;
	canonical_remote_identity: string;
	provider: string | null;
	default_branch: string | null;
	role: string;
	primary: boolean;
	sort_order: number;
	enabled: boolean;
	config: Record<string, unknown>;
	checkouts: Array<{
		checkout_id: string;
		checkout_kind: string;
		canonical: boolean;
		branch: string | null;
		upstream: string | null;
		last_seen_revision: string | null;
		available: boolean;
		last_seen_at: number;
	}>;
}

export interface RepositoryCatalogItem {
	repository_id: string;
	name: string;
	canonical_remote_identity: string;
	provider: string | null;
	default_branch: string | null;
}

export interface WorkspaceInstruction {
	id: string;
	workspace_id: string;
	version: number;
	content: string;
	content_hash: string;
	is_current: boolean;
	change_summary: string | null;
	created_at: number;
}

export interface EnvironmentProfileView {
	profile_id: string;
	workspace_id: string | null;
	name: string;
	description: string | null;
	active_version_id: string | null;
	target_name: string | null;
	is_archived: boolean;
	created_at_ms: number;
	updated_at_ms: number;
	active_version: null | {
		version_id: string;
		version_number: number;
		digest: string;
		runtime_profile: string;
		environment_variable_names: string[];
		package_count: number;
		setting_keys: string[];
		credential_refs: Array<{
			logical_name?: string;
			source_type?: string;
			target_env_var?: string;
			consumers?: string[];
		}>;
		parent_version_id: string | null;
		created_at_ms: number;
	};
}

export interface WorkspaceCheckpointView {
	checkpoint_id: string;
	task_key: string;
	version: number;
	stage: string;
	state: Record<string, unknown>;
	memory_version: number;
	parent_checkpoint_id: string | null;
	created_at_ms: number;
}

export const WORKSPACE_LIVE_EVENT_TYPES = [
	'workspace.created',
	'workspace.updated',
	'workspace.deleted',
	'workspace.context.updated',
	'workspace.context.invalidated',
	'workspace.projection.updated',
	'workspace.revision.changed',
	'workspace.role.changed',
	'workspace.checkout.changed',
	'workspace.checkpoint.changed',
	'workspace.memory.changed',
	'workspace.environment.changed',
	'workspace.instructions.changed'
] as const;

export type WorkspaceLiveEventType = (typeof WORKSPACE_LIVE_EVENT_TYPES)[number];

export interface WorkspaceLiveEvent {
	version: number;
	event_id: string;
	sequence: number;
	timestamp: string;
	target: { type: string; id: string };
	task_id: string | null;
	monitor_id: string | null;
	worker_task_id: string | null;
	type: WorkspaceLiveEventType | string;
	payload: Record<string, unknown>;
	redaction_applied: boolean;
}

export interface WorkspaceProjectionRepository {
	repository_id: string;
	name: string;
	role: string;
	primary: boolean;
	canonical_remote: string;
	default_branch: string | null;
	checkout: null | {
		id: string;
		path: string;
		branch: string | null;
		revision: string | null;
		available: boolean;
	};
}

export interface WorkspaceProjectionWorkbenchSession {
	session_id: string;
	name: string;
	workspace_id: string | null;
	active_workspace_id: string | null;
	active_target_type: string | null;
	active_target_id: string | null;
	event_count: number;
	updated_at: number;
}

export interface WorkspaceContextCacheProjection {
	is_cached: boolean;
	fingerprint: string | null;
	cached_at_ms: number | null;
	expires_at_ms: number | null;
	access_count: number;
	tokens: Record<string, string>;
}

export interface WorkspaceMetricsProjection {
	hits: number;
	misses: number;
	hit_ratio: number;
	evictions: number;
	total_invalidations: number;
	invalidations_by_reason: Record<string, number>;
	events_published: Record<string, number>;
	projections_generated: number;
}

export interface WorkspaceProjection {
	version: number;
	workspace_id: string;
	user_id: string;
	name: string;
	slug: string | null;
	path: string;
	workspace_type: string;
	status: string;
	health: {
		status: string;
		checks: {
			path_configured: boolean;
			path_exists: boolean;
			repositories_total: number;
			repositories_healthy: number;
			has_primary_repo: boolean;
			context_cached: boolean;
			workbench_sessions_active: number;
		};
	};
	repositories: WorkspaceProjectionRepository[];
	workbench: {
		active_sessions_count: number;
		sessions: WorkspaceProjectionWorkbenchSession[];
	};
	context_cache: WorkspaceContextCacheProjection;
	recent_events: WorkspaceLiveEvent[];
	metrics: WorkspaceMetricsProjection;
}

export interface WorkspaceLiveReplay {
	target_key: string;
	after_sequence: number;
	last_sequence: number;
	events: WorkspaceLiveEvent[];
}

export interface WorkspaceStreamSnapshot {
	version: number;
	target: 'workspace';
	snapshot: WorkspaceProjection;
	replay: WorkspaceLiveReplay;
}

export interface WorkspaceStreamCallbacks {
	onOpen?: () => void;
	onSnapshot?: (projection: WorkspaceProjection) => void;
	onEvent?: (event: WorkspaceLiveEvent) => void;
	onError?: (event: Event) => void;
	onProtocolError?: (error: unknown) => void;
}

export async function workspaceOsAction<T>(
	action: WorkspaceOsAction,
	payload: Record<string, unknown> = {}
): Promise<T> {
	return fetchJSON<T>('/api/control/v1/workspace-os/action', jsonBody({ action, payload }));
}

export const getWorkspaceProjection = (workspaceId: string) =>
	fetchJSON<WorkspaceProjection>(
		`/api/control/v1/workspaces/${encodeURIComponent(workspaceId)}/projection`
	);

export const getWorkspaceStreamSnapshot = (workspaceId: string, afterSequence = 0) =>
	fetchJSON<WorkspaceStreamSnapshot>(
		`/api/control/v1/workspaces/${encodeURIComponent(workspaceId)}/stream/snapshot?after=${Math.max(0, Math.trunc(afterSequence))}`
	);

export function openWorkspaceStream(
	workspaceId: string,
	afterSequence: number,
	callbacks: WorkspaceStreamCallbacks
): EventSource {
	if (typeof EventSource === 'undefined') {
		throw new Error('Workspace live stream requires EventSource support');
	}
	const source = new EventSource(
		`/api/control/v1/workspaces/${encodeURIComponent(workspaceId)}/stream?after=${Math.max(0, Math.trunc(afterSequence))}`
	);

	const parseSnapshot = (message: MessageEvent<string>) => {
		try {
			const data = JSON.parse(message.data) as {
				target?: string;
				workspace_id?: string;
				snapshot?: WorkspaceProjection;
			};
			if (!data.snapshot || data.workspace_id !== workspaceId) {
				throw new Error('Workspace stream snapshot does not match the requested workspace');
			}
			callbacks.onSnapshot?.(data.snapshot);
		} catch (error) {
			callbacks.onProtocolError?.(error);
		}
	};

	const parseEvent = (message: MessageEvent<string>) => {
		try {
			const event = JSON.parse(message.data) as WorkspaceLiveEvent;
			if (event.target?.type !== 'workspace' || event.target.id !== workspaceId) {
				throw new Error('Workspace live event target does not match the requested workspace');
			}
			callbacks.onEvent?.(event);
		} catch (error) {
			callbacks.onProtocolError?.(error);
		}
	};

	source.addEventListener('snapshot', (event) => parseSnapshot(event as MessageEvent<string>));
	for (const eventType of WORKSPACE_LIVE_EVENT_TYPES) {
		source.addEventListener(eventType, (event) => parseEvent(event as MessageEvent<string>));
	}
	source.onopen = () => callbacks.onOpen?.();
	source.onerror = (event) => callbacks.onError?.(event);
	return source;
}

export const getWorkspaceHealth = (workspaceId: string) =>
	workspaceOsAction<Record<string, unknown>>('health', { workspace_id: workspaceId });

export const reconcileWorkspace = (workspaceId: string) =>
	workspaceOsAction<Record<string, unknown>>('reconcile', { workspace_id: workspaceId });

export const updateWorkspaceIdentity = (
	workspaceId: string,
	input: { name?: string; slug?: string; workspace_type?: string }
) =>
	workspaceOsAction<{ workspace: WorkspaceSummary }>('workspace_update', {
		workspace_id: workspaceId,
		...input
	});

export const getWorkspaceRepositories = (workspaceId: string) =>
	workspaceOsAction<{
		workspace: WorkspaceSummary;
		repositories: WorkspaceRepositorySummary[];
		repository_count: number;
	}>('repositories', { workspace_id: workspaceId });

export const getRepositoryCatalog = () =>
	workspaceOsAction<{ repositories: RepositoryCatalogItem[] }>('repository_catalog');

export const addWorkspaceRepository = (
	workspaceId: string,
	input: {
		repository_id: string;
		role?: string;
		primary?: boolean;
		sort_order?: number;
		enabled?: boolean;
	}
) =>
	workspaceOsAction<{ repositories: WorkspaceRepositorySummary[] }>('repository_add', {
		workspace_id: workspaceId,
		...input
	});

export const updateWorkspaceRepository = (
	workspaceId: string,
	input: {
		repository_id: string;
		role?: string;
		primary?: boolean;
		sort_order?: number;
		enabled?: boolean;
	}
) =>
	workspaceOsAction<{ repositories: WorkspaceRepositorySummary[] }>('repository_update', {
		workspace_id: workspaceId,
		...input
	});

export const removeWorkspaceRepository = (workspaceId: string, repositoryId: string) =>
	workspaceOsAction<{ repositories: WorkspaceRepositorySummary[] }>('repository_remove', {
		workspace_id: workspaceId,
		repository_id: repositoryId
	});

export const getWorkspaceInstructions = (workspaceId: string) =>
	workspaceOsAction<{ workspace: WorkspaceSummary; instruction: WorkspaceInstruction | null }>(
		'instructions',
		{ workspace_id: workspaceId }
	);

export const getWorkspaceInstructionHistory = (workspaceId: string, limit = 50) =>
	workspaceOsAction<{
		workspace: WorkspaceSummary;
		instructions: WorkspaceInstruction[];
		total: number;
	}>('instruction_history', { workspace_id: workspaceId, limit });

export const saveWorkspaceInstructions = (
	workspaceId: string,
	content: string,
	expectedVersion: number | null,
	changeSummary?: string
) =>
	workspaceOsAction<{ workspace: WorkspaceSummary; instruction: WorkspaceInstruction }>(
		'instruction_save',
		{
			workspace_id: workspaceId,
			content,
			expected_version: expectedVersion,
			change_summary: changeSummary
		}
	);

export const getWorkspaceEnvironmentProfiles = (workspaceId: string) =>
	workspaceOsAction<{ workspace: WorkspaceSummary; profiles: EnvironmentProfileView[] }>(
		'environment_profiles',
		{ workspace_id: workspaceId }
	);

export const createWorkspaceEnvironment = (
	workspaceId: string,
	input: { name: string; description?: string; initial_spec?: Record<string, unknown> }
) =>
	workspaceOsAction<{ workspace: WorkspaceSummary; profile: EnvironmentProfileView }>(
		'environment_create',
		{ workspace_id: workspaceId, ...input }
	);

export const getWorkspaceCheckpoints = (workspaceId: string, limit = 50) =>
	workspaceOsAction<{
		workspace: WorkspaceSummary;
		memory_version: number;
		checkpoints: WorkspaceCheckpointView[];
	}>('checkpoints', { workspace_id: workspaceId, limit });

export const getWorkspaceContext = (workspaceId: string) =>
	workspaceOsAction<{
		workspace: WorkspaceSummary;
		instruction_version: number | null;
		memory_version: number;
		environment_profile: Record<string, unknown> | null;
		context: {
			snapshot_id?: string;
			content_digest?: string;
			diagnostics?: string[];
			[key: string]: unknown;
		};
	}>('context', { workspace_id: workspaceId, max_chars: 9000, memory_max_chars: 3000 });
