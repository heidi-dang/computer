import {
	getWorkspaceProjection,
	getWorkspaceStreamSnapshot,
	openWorkspaceStream
} from '$lib/apis/workspace-os';
import {
	applyWorkspaceLiveEvent,
	applyWorkspaceRecovery,
	createWorkspaceOsCoreState,
	hydrateWorkspaceProjection,
	markWorkspaceConnection,
	markWorkspaceDomainsFresh,
	markWorkspaceDomainsStale,
	type WorkspaceOsCoreState,
	type WorkspaceOsDomain
} from '$lib/stores/workspace-os-state';

const RECOVERY_PAGE_SIZE = 200;
const MAX_RECOVERY_PAGES = 20;
const PROJECTION_REFRESH_DELAY_MS = 75;
const MAX_RECONNECT_DELAY_MS = 5_000;

let _state = $state<WorkspaceOsCoreState>(createWorkspaceOsCoreState());
let _source: EventSource | null = null;
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _projectionRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let _generation = 0;
let _connectPromise: Promise<void> | null = null;

function errorMessage(error: unknown): string {
	if (error instanceof Error && error.message) return error.message;
	if (typeof error === 'string' && error) return error;
	return 'Workspace live state unavailable';
}

function clearTimers() {
	if (_reconnectTimer) {
		clearTimeout(_reconnectTimer);
		_reconnectTimer = null;
	}
	if (_projectionRefreshTimer) {
		clearTimeout(_projectionRefreshTimer);
		_projectionRefreshTimer = null;
	}
}

function closeSource() {
	const source = _source;
	_source = null;
	source?.close();
}

async function refreshProjectionFor(workspaceId: string, generation: number) {
	try {
		const projection = await getWorkspaceProjection(workspaceId);
		if (generation !== _generation || _state.workspaceId !== workspaceId) return;
		_state = hydrateWorkspaceProjection(_state, projection);
	} catch {
		// The live stream remains authoritative for liveness. Keep the affected
		// domains stale and retry projection hydration on the next live event.
	}
}

function scheduleProjectionRefresh(workspaceId: string, generation: number) {
	if (_projectionRefreshTimer) clearTimeout(_projectionRefreshTimer);
	_projectionRefreshTimer = setTimeout(() => {
		_projectionRefreshTimer = null;
		void refreshProjectionFor(workspaceId, generation);
	}, PROJECTION_REFRESH_DELAY_MS);
}

async function recoverFromCursor(
	workspaceId: string,
	generation: number,
	afterSequence: number
): Promise<boolean> {
	let cursor = Math.max(0, afterSequence);
	for (let page = 0; page < MAX_RECOVERY_PAGES; page += 1) {
		const recovery = await getWorkspaceStreamSnapshot(workspaceId, cursor);
		if (generation !== _generation || _state.workspaceId !== workspaceId) return false;

		_state = applyWorkspaceRecovery(_state, recovery);
		const nextCursor = _state.connection.lastSequence;
		if (recovery.replay.events.length < RECOVERY_PAGE_SIZE || nextCursor <= cursor) {
			return true;
		}
		cursor = nextCursor;
	}

	// A very large replay gap is safe to collapse to the authoritative current
	// projection as long as all detail domains are marked stale before the live
	// stream resumes. Lazy detail surfaces will then refresh instead of trusting
	// potentially skipped invalidation events.
	_state = markWorkspaceDomainsStale(_state);
	return true;
}

function scheduleRecovery(workspaceId: string, generation: number, reason: unknown) {
	if (generation !== _generation || _state.workspaceId !== workspaceId || _reconnectTimer) return;
	closeSource();
	const attempts = _state.connection.reconnectAttempts + 1;
	_state = markWorkspaceConnection(_state, 'reconnecting', {
		error: errorMessage(reason),
		reconnectAttempts: attempts
	});
	const delay = Math.min(MAX_RECONNECT_DELAY_MS, 250 * 2 ** Math.min(attempts - 1, 5));
	_reconnectTimer = setTimeout(() => {
		_reconnectTimer = null;
		void recoverAndReconnect(workspaceId, generation);
	}, delay);
}

function openStream(workspaceId: string, generation: number) {
	if (generation !== _generation || _state.workspaceId !== workspaceId) return;
	closeSource();
	try {
		_source = openWorkspaceStream(workspaceId, _state.connection.lastSequence, {
			onOpen: () => {
				if (generation !== _generation || _state.workspaceId !== workspaceId) return;
				_state = markWorkspaceConnection(_state, 'live', {
					error: null,
					reconnectAttempts: 0
				});
			},
			onSnapshot: (projection) => {
				if (generation !== _generation || _state.workspaceId !== workspaceId) return;
				_state = hydrateWorkspaceProjection(_state, projection);
			},
			onEvent: (event) => {
				if (generation !== _generation || _state.workspaceId !== workspaceId) return;
				const previousSequence = _state.connection.lastSequence;
				_state = applyWorkspaceLiveEvent(_state, event);
				if (_state.connection.lastSequence > previousSequence) {
					scheduleProjectionRefresh(workspaceId, generation);
				}
			},
			onProtocolError: (error) => {
				scheduleRecovery(workspaceId, generation, error);
			},
			onError: (event) => {
				scheduleRecovery(workspaceId, generation, event);
			}
		});
	} catch (error) {
		scheduleRecovery(workspaceId, generation, error);
	}
}

async function recoverAndReconnect(workspaceId: string, generation: number) {
	if (generation !== _generation || _state.workspaceId !== workspaceId) return;
	try {
		await recoverFromCursor(workspaceId, generation, _state.connection.lastSequence);
		if (generation !== _generation || _state.workspaceId !== workspaceId) return;
		openStream(workspaceId, generation);
	} catch (error) {
		scheduleRecovery(workspaceId, generation, error);
	}
}

async function bootstrap(workspaceId: string, generation: number) {
	try {
		await recoverFromCursor(workspaceId, generation, 0);
		if (generation !== _generation || _state.workspaceId !== workspaceId) return;
		openStream(workspaceId, generation);
	} catch (error) {
		if (generation !== _generation || _state.workspaceId !== workspaceId) return;
		_state = markWorkspaceConnection(_state, 'failed', {
			error: errorMessage(error),
			reconnectAttempts: 0
		});
	}
}

export const workspaceOsStore = {
	get state() {
		return _state;
	},
	get workspaceId() {
		return _state.workspaceId;
	},
	get projection() {
		return _state.projection;
	},
	get connection() {
		return _state.connection;
	},
	get staleDomains() {
		return _state.staleDomains;
	},

	connect(workspaceId: string) {
		const normalized = workspaceId.trim();
		if (!normalized) {
			this.disconnect();
			return Promise.resolve();
		}
		if (_state.workspaceId === normalized) {
			if (_connectPromise) return _connectPromise;
			if (
				_state.connection.status === 'live' ||
				_state.connection.status === 'connecting' ||
				_state.connection.status === 'reconnecting'
			) {
				return Promise.resolve();
			}
		}

		clearTimers();
		closeSource();
		const generation = ++_generation;
		_state = createWorkspaceOsCoreState(normalized);
		_connectPromise = bootstrap(normalized, generation).finally(() => {
			if (generation === _generation) _connectPromise = null;
		});
		return _connectPromise;
	},

	disconnect() {
		_generation += 1;
		clearTimers();
		closeSource();
		_connectPromise = null;
		_state = createWorkspaceOsCoreState();
	},

	retry() {
		const workspaceId = _state.workspaceId;
		if (!workspaceId) return Promise.resolve();
		clearTimers();
		closeSource();
		const generation = ++_generation;
		_state = markWorkspaceConnection(_state, 'connecting', {
			error: null,
			reconnectAttempts: 0
		});
		_connectPromise = bootstrap(workspaceId, generation).finally(() => {
			if (generation === _generation) _connectPromise = null;
		});
		return _connectPromise;
	},

	refresh() {
		const workspaceId = _state.workspaceId;
		if (!workspaceId) return Promise.resolve();
		return refreshProjectionFor(workspaceId, _generation);
	},

	markFresh(domains: readonly WorkspaceOsDomain[]) {
		_state = markWorkspaceDomainsFresh(_state, domains);
	}
};
