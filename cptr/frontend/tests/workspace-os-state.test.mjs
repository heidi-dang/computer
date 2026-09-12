import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const stateSource = await readFile(
	new URL('../src/lib/stores/workspace-os-state.ts', import.meta.url),
	'utf8'
);
const transpiled = ts.transpileModule(stateSource, {
	compilerOptions: {
		module: ts.ModuleKind.ES2022,
		target: ts.ScriptTarget.ES2022
	}
}).outputText;
const stateModule = await import(
	`data:text/javascript;base64,${Buffer.from(transpiled).toString('base64')}`
);

const {
	applyWorkspaceLiveEvent,
	applyWorkspaceRecovery,
	createWorkspaceOsCoreState,
	hydrateWorkspaceProjection,
	markWorkspaceDomainsFresh
} = stateModule;

function liveEvent(sequence, type, workspaceId = 'ws-1') {
	return {
		version: 1,
		event_id: `event-${sequence}`,
		sequence,
		timestamp: '2026-09-12T00:00:00+00:00',
		target: { type: 'workspace', id: workspaceId },
		task_id: null,
		monitor_id: null,
		worker_task_id: null,
		type,
		payload: { workspace_id: workspaceId },
		redaction_applied: true
	};
}

function projection(recentEvents = []) {
	return {
		version: 1,
		workspace_id: 'ws-1',
		user_id: 'user-1',
		name: 'Cross Repo',
		slug: 'cross-repo',
		path: '/srv/cross-repo',
		workspace_type: 'multi_repo',
		status: 'active',
		health: {
			status: 'active',
			checks: {
				path_configured: true,
				path_exists: true,
				repositories_total: 1,
				repositories_healthy: 1,
				has_primary_repo: true,
				context_cached: true,
				workbench_sessions_active: 0
			}
		},
		repositories: [],
		workbench: { active_sessions_count: 0, sessions: [] },
		context_cache: {
			is_cached: true,
			fingerprint: 'ctx',
			cached_at_ms: 1,
			expires_at_ms: 2,
			access_count: 1,
			tokens: {}
		},
		recent_events: recentEvents,
		metrics: {
			hits: 1,
			misses: 0,
			hit_ratio: 1,
			evictions: 0,
			total_invalidations: 0,
			invalidations_by_reason: {},
			events_published: {},
			projections_generated: 1
		},
		fingerprint: 'projection',
		generated_at_ms: 1
	};
}

test('projection hydration never advances the durable replay cursor', () => {
	const state = createWorkspaceOsCoreState('ws-1');
	const hydrated = hydrateWorkspaceProjection(
		state,
		projection([liveEvent(4, 'workspace.updated')])
	);

	assert.equal(hydrated.workspaceId, 'ws-1');
	assert.equal(hydrated.projection?.name, 'Cross Repo');
	assert.equal(hydrated.connection.lastSequence, 0);
});

test('live reducer rejects duplicate and foreign events and marks affected domains stale', () => {
	let state = hydrateWorkspaceProjection(createWorkspaceOsCoreState('ws-1'), projection());
	for (let sequence = 1; sequence <= 4; sequence += 1) {
		state = applyWorkspaceLiveEvent(state, liveEvent(sequence, 'workspace.updated'));
	}

	const duplicate = applyWorkspaceLiveEvent(state, liveEvent(4, 'workspace.instructions.changed'));
	assert.equal(duplicate, state);

	const foreign = applyWorkspaceLiveEvent(state, liveEvent(5, 'workspace.updated', 'ws-2'));
	assert.equal(foreign, state);

	state = applyWorkspaceLiveEvent(state, liveEvent(5, 'workspace.instructions.changed'));
	assert.equal(state.connection.lastSequence, 5);
	assert.ok(state.staleDomains.includes('projection'));
	assert.ok(state.staleDomains.includes('instructions'));
	assert.ok(state.staleDomains.includes('context'));
	assert.equal(state.projection?.recent_events.at(-1)?.event_id, 'event-5');
});

test('a sequence gap fails conservative by marking every Workspace OS domain stale', () => {
	const state = hydrateWorkspaceProjection(createWorkspaceOsCoreState('ws-1'), projection());
	const gapped = applyWorkspaceLiveEvent(state, liveEvent(3, 'workspace.environment.changed'));

	assert.equal(gapped.connection.lastSequence, 3);
	assert.ok(gapped.staleDomains.includes('environment'));
	assert.ok(gapped.staleDomains.includes('tasks'));
	assert.ok(gapped.staleDomains.includes('privilege'));
});

test('loaded Workspace Center sections can clear only their own stale domains', () => {
	let state = hydrateWorkspaceProjection(createWorkspaceOsCoreState('ws-1'), projection());
	state = applyWorkspaceLiveEvent(state, liveEvent(2, 'workspace.instructions.changed'));
	assert.ok(state.staleDomains.includes('instructions'));
	assert.ok(state.staleDomains.includes('context'));

	state = markWorkspaceDomainsFresh(state, ['instructions']);
	assert.ok(!state.staleDomains.includes('instructions'));
	assert.ok(state.staleDomains.includes('context'));
});

test('recovery sorts durable replay, applies each missing event once, and trusts replay cursor', () => {
	let state = hydrateWorkspaceProjection(createWorkspaceOsCoreState('ws-1'), projection());
	for (let sequence = 1; sequence <= 5; sequence += 1) {
		state = applyWorkspaceLiveEvent(state, liveEvent(sequence, 'workspace.updated'));
	}

	const recovery = {
		version: 1,
		target: 'workspace',
		snapshot: projection([]),
		replay: {
			target_key: 'workspace:ws-1',
			after_sequence: 5,
			last_sequence: 9,
			events: [
				liveEvent(9, 'workspace.environment.changed'),
				liveEvent(7, 'workspace.checkpoint.changed'),
				liveEvent(8, 'workspace.updated'),
				liveEvent(6, 'workspace.updated')
			]
		}
	};

	state = applyWorkspaceRecovery(state, recovery);
	assert.equal(state.connection.lastSequence, 9);
	assert.ok(state.staleDomains.includes('environment'));
	assert.ok(state.staleDomains.includes('checkpoints'));
	assert.ok(state.staleDomains.includes('context'));
});
