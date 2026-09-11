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

export async function workspaceOsAction<T>(
	action: WorkspaceOsAction,
	payload: Record<string, unknown> = {}
): Promise<T> {
	return fetchJSON<T>('/api/control/v1/workspace-os/action', jsonBody({ action, payload }));
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
