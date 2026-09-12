import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('first-open workspace hydration creates or preserves a stable UUID before exposing state', async () => {
	const stores = await read('lib/stores.ts');
	const stateApi = await read('lib/apis/state.ts');

	assert.match(stores, /workspace_id:\s*string;/);
	assert.match(stores, /if \(!workspaceId\)\s*\{/);
	assert.match(
		stores,
		/await saveWorkspaceState\(canonicalWorkspacePath, \{ name: workspaceName \}\)/
	);
	assert.match(stores, /workspace_id:\s*workspaceId/);
	assert.match(stores, /workspaceOsStore\.connect\(workspaceId\)/);
	assert.match(stateApi, /fetchJSON<WorkspaceSaveResponse>/);
});

test('Workspace OS API exposes typed projection and cursor-based named SSE events', async () => {
	const api = await read('lib/apis/workspace-os.ts');

	assert.match(api, /export interface WorkspaceProjection/);
	assert.match(api, /export interface WorkspaceLiveEvent/);
	assert.match(api, /getWorkspaceStreamSnapshot/);
	assert.match(api, /\/stream\/snapshot\?after=/);
	assert.match(api, /\/stream\?after=/);
	assert.match(api, /workspace\.instructions\.changed/);
	assert.match(api, /source\.addEventListener\('snapshot'/);
	assert.match(api, /for \(const eventType of WORKSPACE_LIVE_EVENT_TYPES\)/);
});

test('WorkspaceOsStore performs explicit replay recovery and sequence-aware hydration', async () => {
	const store = await read('lib/stores/workspace-os.svelte.ts');
	const reducer = await read('lib/stores/workspace-os-state.ts');

	assert.match(store, /recoverFromCursor/);
	assert.match(store, /MAX_RECOVERY_PAGES/);
	assert.match(store, /getWorkspaceStreamSnapshot\(workspaceId, cursor\)/);
	assert.match(store, /openWorkspaceStream\(workspaceId, _state\.connection\.lastSequence/);
	assert.match(store, /markWorkspaceDomainsStale/);
	assert.match(reducer, /event\.sequence <= state\.connection\.lastSequence/);
	assert.match(reducer, /recovery\.replay\.events.*sort/s);
});

test('secondary API 401 cannot blindly logout a still-authenticated browser session', async () => {
	const api = await read('lib/apis/index.ts');

	assert.match(api, /browserSessionIsInvalid/);
	assert.match(api, /fetch\('\/api\/auth', \{ credentials: 'include' \}\)/);
	assert.match(api, /payload\?\.authenticated === false/);
	assert.match(api, /await browserSessionIsInvalid\(\)/);
	assert.match(api, /clearSession\(\)/);
	assert.doesNotMatch(api, /if \(res\.status === 401[^}]*\{\s*clearSession\(\);\s*\}/s);
});
