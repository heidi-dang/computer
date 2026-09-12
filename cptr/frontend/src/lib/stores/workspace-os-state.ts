import type {
	WorkspaceLiveEvent,
	WorkspaceProjection,
	WorkspaceStreamSnapshot
} from '$lib/apis/workspace-os';

export type WorkspaceOsDomain =
	| 'projection'
	| 'repositories'
	| 'instructions'
	| 'environment'
	| 'memory'
	| 'context'
	| 'checkpoints'
	| 'health'
	| 'workbench'
	| 'tasks'
	| 'privilege';

export type WorkspaceOsConnectionStatus =
	'idle' | 'connecting' | 'live' | 'reconnecting' | 'failed';

export interface WorkspaceOsConnectionState {
	status: WorkspaceOsConnectionStatus;
	lastSequence: number;
	lastEventAt: number | null;
	reconnectAttempts: number;
	error: string | null;
}

export interface WorkspaceOsCoreState {
	workspaceId: string | null;
	projection: WorkspaceProjection | null;
	connection: WorkspaceOsConnectionState;
	staleDomains: WorkspaceOsDomain[];
}

const PROJECTION_REFRESHED_DOMAINS: WorkspaceOsDomain[] = [
	'projection',
	'repositories',
	'health',
	'workbench'
];

const ALL_DOMAINS: WorkspaceOsDomain[] = [
	'projection',
	'repositories',
	'instructions',
	'environment',
	'memory',
	'context',
	'checkpoints',
	'health',
	'workbench',
	'tasks',
	'privilege'
];

export function createWorkspaceOsCoreState(
	workspaceId: string | null = null
): WorkspaceOsCoreState {
	return {
		workspaceId,
		projection: null,
		connection: {
			status: workspaceId ? 'connecting' : 'idle',
			lastSequence: 0,
			lastEventAt: null,
			reconnectAttempts: 0,
			error: null
		},
		staleDomains: []
	};
}

function uniqueDomains(
	current: WorkspaceOsDomain[],
	add: readonly WorkspaceOsDomain[]
): WorkspaceOsDomain[] {
	return Array.from(new Set<WorkspaceOsDomain>([...current, ...add]));
}

function clearDomains(
	current: WorkspaceOsDomain[],
	remove: readonly WorkspaceOsDomain[]
): WorkspaceOsDomain[] {
	const blocked = new Set<WorkspaceOsDomain>(remove);
	return current.filter((domain) => !blocked.has(domain));
}

export function domainsForWorkspaceEvent(type: string): WorkspaceOsDomain[] {
	switch (type) {
		case 'workspace.instructions.changed':
			return ['projection', 'instructions', 'context'];
		case 'workspace.environment.changed':
			return ['projection', 'environment', 'context'];
		case 'workspace.memory.changed':
			return ['projection', 'memory', 'context'];
		case 'workspace.checkpoint.changed':
			return ['projection', 'checkpoints', 'context'];
		case 'workspace.revision.changed':
		case 'workspace.role.changed':
		case 'workspace.checkout.changed':
			return ['projection', 'repositories', 'context', 'health'];
		case 'workspace.context.updated':
		case 'workspace.context.invalidated':
			return ['projection', 'context'];
		case 'workspace.created':
		case 'workspace.updated':
		case 'workspace.deleted':
		case 'workspace.projection.updated':
			return ['projection', 'repositories', 'health', 'workbench'];
		default:
			return ['projection'];
	}
}

export function hydrateWorkspaceProjection(
	state: WorkspaceOsCoreState,
	projection: WorkspaceProjection
): WorkspaceOsCoreState {
	if (state.workspaceId && state.workspaceId !== projection.workspace_id) {
		return state;
	}
	return {
		...state,
		workspaceId: projection.workspace_id,
		projection,
		connection: {
			...state.connection,
			error: null
		},
		staleDomains: clearDomains(state.staleDomains, PROJECTION_REFRESHED_DOMAINS)
	};
}

export function applyWorkspaceLiveEvent(
	state: WorkspaceOsCoreState,
	event: WorkspaceLiveEvent
): WorkspaceOsCoreState {
	if (
		event.target?.type !== 'workspace' ||
		(state.workspaceId !== null && event.target.id !== state.workspaceId)
	) {
		return state;
	}
	if (!Number.isFinite(event.sequence) || event.sequence <= state.connection.lastSequence) {
		return state;
	}
	const sequenceGap = event.sequence > state.connection.lastSequence + 1;

	const projection = state.projection
		? {
				...state.projection,
				recent_events: [
					...state.projection.recent_events.filter((item) => item.event_id !== event.event_id),
					event
				].slice(-50)
			}
		: null;

	return {
		...state,
		workspaceId: state.workspaceId ?? event.target.id,
		projection,
		connection: {
			...state.connection,
			lastSequence: event.sequence,
			lastEventAt: Date.parse(event.timestamp) || Date.now(),
			error: null
		},
		staleDomains: uniqueDomains(
			state.staleDomains,
			sequenceGap ? ALL_DOMAINS : domainsForWorkspaceEvent(event.type)
		)
	};
}

export function applyWorkspaceRecovery(
	state: WorkspaceOsCoreState,
	recovery: WorkspaceStreamSnapshot
): WorkspaceOsCoreState {
	if (
		recovery.target !== 'workspace' ||
		(state.workspaceId !== null && recovery.snapshot.workspace_id !== state.workspaceId)
	) {
		return state;
	}

	let next = hydrateWorkspaceProjection(state, recovery.snapshot);
	const replay = [...recovery.replay.events].sort((a, b) => a.sequence - b.sequence);
	for (const event of replay) {
		next = applyWorkspaceLiveEvent(next, event);
	}

	return {
		...next,
		connection: {
			...next.connection,
			lastSequence: Math.max(next.connection.lastSequence, recovery.replay.last_sequence),
			error: null
		}
	};
}

export function markWorkspaceDomainsStale(
	state: WorkspaceOsCoreState,
	domains: readonly WorkspaceOsDomain[] = ALL_DOMAINS
): WorkspaceOsCoreState {
	return {
		...state,
		staleDomains: uniqueDomains(state.staleDomains, domains)
	};
}

export function markWorkspaceDomainsFresh(
	state: WorkspaceOsCoreState,
	domains: readonly WorkspaceOsDomain[]
): WorkspaceOsCoreState {
	return {
		...state,
		staleDomains: clearDomains(state.staleDomains, domains)
	};
}

export function markWorkspaceConnection(
	state: WorkspaceOsCoreState,
	status: WorkspaceOsConnectionStatus,
	options: {
		error?: string | null;
		reconnectAttempts?: number;
	} = {}
): WorkspaceOsCoreState {
	return {
		...state,
		connection: {
			...state.connection,
			status,
			error: options.error === undefined ? state.connection.error : options.error,
			reconnectAttempts:
				options.reconnectAttempts === undefined
					? state.connection.reconnectAttempts
					: options.reconnectAttempts
		}
	};
}
